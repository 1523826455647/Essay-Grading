"""批改记录对话：用户可就某条批改记录与 AI 展开多轮对话，学习理解答案问题与改进方向。

接口：
- GET  /api/chat/records          当前用户近期批改记录列表
- POST /api/chat/stream           SSE 流式对话（body: {"sid": ..., "messages": [{"role","content"}, ...]})
"""

import json
import logging
import time

import requests
from flask import Blueprint, Response, request, stream_with_context

from src.api.utils import api_success, api_error, token_required, get_db
from src.services.feature_model_service import get_feature_model
from src.services.grader.llm_client import build_chat_completions_endpoint

logger = logging.getLogger(__name__)

grading_chat_bp = Blueprint('grading_chat', __name__, url_prefix='/api/chat')

RECORD_LIMIT = 50          # 近期记录上限
MAX_CONTEXT_CHARS = 6000   # 用户答案/参考答案注入上限，避免超长上下文
MAX_MATERIAL_CHARS = 9000  # 给定材料注入上限（材料较长时截断，保留开头）


def _loads(value, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


# ---------------------------------------------------------------------------
# 1. 近期批改记录列表
# ---------------------------------------------------------------------------
@grading_chat_bp.route('/records', methods=['GET'])
@token_required
def list_records(current_user):
    db = get_db()
    rows = db.execute(
        """
        SELECT s.sid, s.pid, s.qid, s.user_answer, s.score, s.graded_at, s.created_at,
               p.title, p.questions
        FROM submissions s
        LEFT JOIN papers p ON s.pid = p.pid
        WHERE s.uid = ? AND s.score IS NOT NULL
        ORDER BY s.created_at DESC
        LIMIT ?
        """,
        (current_user['uid'], RECORD_LIMIT),
    ).fetchall()

    records = []
    for r in rows:
        question = None
        questions = _loads(r['questions'], [])
        if isinstance(questions, list):
            for q in questions:
                if q.get('qid') == r['qid']:
                    question = q
                    break
        stem = (question or {}).get('stem') or ''
        qtype = (question or {}).get('type') or '未分类'
        score_max = (question or {}).get('score_max') or 0
        # 去掉 score_max 里的分数字样（如 "15分"）
        try:
            score_max = float(score_max) if score_max else 0
        except (TypeError, ValueError):
            score_max = 0
        records.append({
            'sid': r['sid'],
            'pid': r['pid'],
            'qid': r['qid'],
            'title': r['title'] or '',
            'question_type': qtype,
            'stem': stem,
            'score': r['score'],
            'score_max': score_max,
            'answer_len': len(r['user_answer'] or ''),
            'created_at': r['created_at'] or r['graded_at'] or '',
        })
    return api_success({'records': records})



# ---------------------------------------------------------------------------
# 1.2 删除批改记录（含关联数据，不可恢复）
# ---------------------------------------------------------------------------
@grading_chat_bp.route('/records/<sid>', methods=['DELETE'])
@token_required
def delete_record(current_user, sid):
    """删除一条批改记录及其关联数据。

    级联清理：
    - chat_messages：该记录的对话历史
    - submission_judgments：多模型评委明细
    - question_type_drills：题型训练记录（不 JOIN 查询，可安全清理）
    保留 token_usage_logs（平台成本账，属运营数据，不随用户删除而消失）。
    """
    db = get_db()
    row = db.execute(
        "SELECT sid, pid, qid, score FROM submissions WHERE sid = ? AND uid = ?",
        (sid, current_user['uid']),
    ).fetchone()
    if not row:
        return api_error('批改记录不存在或无权删除', 404)

    try:
        db.execute(
            "DELETE FROM chat_messages WHERE uid = ? AND sid = ?",
            (current_user['uid'], sid),
        )
        db.execute("DELETE FROM submission_judgments WHERE sid = ?", (sid,))
        db.execute(
            "DELETE FROM question_type_drills WHERE uid = ? AND sid = ?",
            (current_user['uid'], sid),
        )
        db.execute(
            "DELETE FROM submissions WHERE sid = ? AND uid = ?",
            (sid, current_user['uid']),
        )
        db.commit()
    except Exception:
        logger.exception("delete submission failed: sid=%s", sid)
        return api_error('删除失败，请稍后重试', 500)

    logger.info(
        "submission deleted: sid=%s pid=%s qid=%s uid=%s",
        sid, row['pid'], row['qid'], current_user['uid'],
    )
    return api_success({'deleted': True, 'sid': sid})


# ---------------------------------------------------------------------------
# 1.5 对话消息存取（服务器端存储，替代 localStorage）
# ---------------------------------------------------------------------------
@grading_chat_bp.route('/messages/<sid>', methods=['GET'])
@token_required
def get_messages(current_user, sid):
    """获取某条批改记录的所有对话消息"""
    db = get_db()
    # 验证 sid 归属
    owner = db.execute(
        "SELECT 1 FROM submissions WHERE sid = ? AND uid = ?",
        (sid, current_user['uid']),
    ).fetchone()
    if not owner:
        return api_error("批改记录不存在或无权访问", 404)

    rows = db.execute(
        "SELECT role, content, created_at FROM chat_messages "
        "WHERE uid = ? AND sid = ? ORDER BY id ASC",
        (current_user['uid'], sid),
    ).fetchall()
    messages = [{'role': r['role'], 'content': r['content']} for r in rows]
    return api_success({'messages': messages})


@grading_chat_bp.route('/messages/<sid>', methods=['POST'])
@token_required
def save_messages(current_user, sid):
    """保存/替换对话消息（全量覆盖）"""
    db = get_db()
    body = request.get_json(silent=True) or {}
    messages = body.get('messages') or []
    if not isinstance(messages, list):
        return api_error("消息格式错误", 400)

    # 验证 sid 归属
    owner = db.execute(
        "SELECT 1 FROM submissions WHERE sid = ? AND uid = ?",
        (sid, current_user['uid']),
    ).fetchone()
    if not owner:
        return api_error("批改记录不存在或无权访问", 404)

    # 全量覆盖：删除旧消息，写入新消息
    db.execute(
        "DELETE FROM chat_messages WHERE uid = ? AND sid = ?",
        (current_user['uid'], sid),
    )
    for m in messages:
        role = m.get('role', '')
        content = str(m.get('content', ''))
        if role not in ('user', 'assistant') or not content.strip():
            continue
        db.execute(
            "INSERT INTO chat_messages (uid, sid, role, content) VALUES (?, ?, ?, ?)",
            (current_user['uid'], sid, role, content[:8000]),
        )
    db.commit()
    return api_success({'saved': len(messages)})


@grading_chat_bp.route('/messages/<sid>', methods=['DELETE'])
@token_required
def clear_messages(current_user, sid):
    """清空某条批改记录的对话消息"""
    db = get_db()
    db.execute(
        "DELETE FROM chat_messages WHERE uid = ? AND sid = ?",
        (current_user['uid'], sid),
    )
    db.commit()
    return api_success({'cleared': True})


# ---------------------------------------------------------------------------
# 2. 流式对话
# ---------------------------------------------------------------------------
def _build_context(record: dict, question: dict, answer_keys: dict, material: list = None) -> str:
    """把批改记录组装成给模型的上下文文本（含材料、题目、答案、批改结果）。"""
    parts = []
    parts.append(f"【试卷】{record.get('title') or ''}")
    parts.append(f"【题目类型】{question.get('type') or '未分类'}")

    score_max = record.get('score_max') or question.get('score_max') or 100
    parts.append(f"【题目】{question.get('stem') or ''}")
    # 题目附加要求（字数、文种、作答范围等），模型需要这些才能准确讲解
    if question.get('word_limit'):
        parts.append(f"【字数要求】{question.get('word_limit')}")
    if question.get('requirement'):
        parts.append(f"【作答要求】{question.get('requirement')}")
    if question.get('document_type'):
        parts.append(f"【文种】{question.get('document_type')}")
    material_scope = question.get('material_scope')
    if material_scope and material_scope != '全部材料':
        parts.append(f"【材料范围】{material_scope}")
    parts.append(f"【本题分值】{score_max}分")

    # 给定材料：材料类题型讲解必须看到材料，否则无法说明"哪些点在材料里"
    if material:
        mat_text = []
        for i, seg in enumerate(material, 1):
            seg = str(seg or '').strip()
            if seg:
                mat_text.append(f"[材料{i}] {seg}")
        if mat_text:
            joined = "\n".join(mat_text)
            parts.append(f"【给定材料】\n{joined[:MAX_MATERIAL_CHARS]}")

    ref = answer_keys.get(record['qid']) if isinstance(answer_keys, dict) else None
    if isinstance(ref, (list, dict)):
        ref = json.dumps(ref, ensure_ascii=False)
    if ref and str(ref).strip():
        parts.append(f"【参考答案】\n{str(ref).strip()[:MAX_CONTEXT_CHARS]}")

    user_ans = (record.get('user_answer') or '').strip()
    if user_ans:
        parts.append(f"【用户答案】\n{user_ans[:MAX_CONTEXT_CHARS]}")

    score = record.get('score')
    if score is not None:
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = None
    if score is not None:
        parts.append(f"【得分】{score:.1f} 分（满分 {score_max} 分）")

    dims = _loads(record.get('dimension_scores'), {})
    if isinstance(dims, dict) and dims:
        dim_text = "、".join(f"{k}: {v}" for k, v in dims.items())
        parts.append(f"【各维度得分】{dim_text}")

    hit = _loads(record.get('hit_points'), [])
    if isinstance(hit, list) and hit:
        lines = []
        for i, p in enumerate(hit[:12], 1):
            point = p.get('point') if isinstance(p, dict) else str(p)
            lines.append(f"{i}. {point}")
        parts.append("【命中采分点】\n" + "\n".join(lines))

    missing = _loads(record.get('missing_points'), [])
    if isinstance(missing, list) and missing:
        lines = []
        for i, p in enumerate(missing[:12], 1):
            point = p.get('point') if isinstance(p, dict) else str(p)
            lines.append(f"{i}. {point}")
        parts.append("【遗漏采分点】\n" + "\n".join(lines))

    feedback = (record.get('ai_feedback') or '').strip()
    if not feedback:
        agg = _loads(record.get('aggregate_json'), {})
        if isinstance(agg, dict):
            feedback = (agg.get('ai_feedback') or '').strip()
    if feedback:
        parts.append(f"【AI 批改评语】\n{feedback[:MAX_CONTEXT_CHARS]}")

    suggestions = _loads(record.get('improving_suggestions'), [])
    if isinstance(suggestions, list) and suggestions:
        lines = [f"{i + 1}. {s}" for i, s in enumerate(suggestions[:10]) if str(s).strip()]
        if lines:
            parts.append("【改进建议】\n" + "\n".join(lines))

    return "\n\n".join(parts)


SYSTEM_TEMPLATE = """你是「申论帮」的批改讲解助手。下面是用户一次申论作答的完整批改记录，包含给定材料、题目要求、参考答案、用户答案与得分，请基于这份记录回答用户的提问，帮助用户真正理解材料要点、自己答案的问题、为什么扣分、如何改进。

【批改记录开始】
{context}
【批改记录结束】

回答要求：
1. 紧扣给定材料和题目要求回答；讲解时引用材料原句/原词和用户答案原句，具体指出哪些采分点来自材料哪部分、用户是否踩到；
2. 结合参考答案说明这类题的正确作答思路（结构、逻辑、采分点），解释"为什么答案要这样写、这些点在材料里怎么找"；
3. 给出可操作的改进建议，最好附带结合材料的修改示例；
4. 使用 Markdown 排版（加粗、列表、必要时分点），语气友好专业；
5. 若用户的问题超出这份记录的范围，基于你的申论知识回答，并简要说明与本题材料的关联。"""


def _chat_model_config():
    """优先取批改对话绑定模型，其次取批改模型，最后回退全局配置。"""
    model = get_feature_model('grading_chat') or get_feature_model('grading')
    if model:
        return model
    from src.config import get_llm_config
    return get_llm_config()


def _model_call_params(cfg: dict) -> dict:
    base_url = (cfg.get('base_url') or '').strip()
    model_name = (cfg.get('model_name') or cfg.get('model') or '').strip()
    api_key = (cfg.get('api_key') or '').strip()
    return {
        'endpoint': build_chat_completions_endpoint(base_url),
        'model_name': model_name,
        'api_key': api_key,
    }


def _stream_chat(model_cfg: dict, messages: list, temperature: float = 0.4, max_tokens: int = 0):
    """调用 OpenAI 兼容接口的流式补全，逐块 yield 文本。"""
    p = _model_call_params(model_cfg)
    if not p['api_key'] or not p['model_name']:
        yield "\n\n> 模型未配置（缺少 API Key 或模型名），请联系管理员在后台绑定模型。"
        return

    body = {
        'model': p['model_name'],
        'messages': messages,
        'temperature': temperature,
        'stream': True,
    }
    if max_tokens and max_tokens > 0:
        body['max_tokens'] = max_tokens
    try:
        resp = requests.post(
            p['endpoint'],
            headers={
                'Content-Type': 'application/json',
                'Authorization': f"Bearer {p['api_key']}",
            },
            json=body,
            stream=True,
            timeout=(10, 300),
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("chat stream request failed: %s", e)
        yield "\n\n> 模型服务暂时不可用，请稍后重试。"
        return

    try:
        for raw in resp.iter_lines():
            if not raw:
                continue
            raw = raw.decode('utf-8', 'replace').strip()
            if not raw.startswith('data:'):
                continue
            data = raw[5:].strip()
            if data == '[DONE]':
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = chunk.get('choices') or []
            if not choices:
                continue
            delta = choices[0].get('delta') or {}
            piece = delta.get('content')
            if piece:
                yield piece
    except requests.RequestException as e:
        logger.warning("chat stream interrupted: %s", e)
        yield "\n\n> 网络中断，回复被截断。"
    finally:
        try:
            resp.close()
        except Exception:
            pass


@grading_chat_bp.route('/stream', methods=['POST'])
@token_required
def stream_chat(current_user):
    body = request.get_json(silent=True) or {}
    sid = (body.get('sid') or '').strip()
    messages = body.get('messages') or []

    if not sid:
        return api_error('缺少批改记录 sid', 400)
    if not isinstance(messages, list) or not messages:
        return api_error('缺少对话消息', 400)
    # 只保留 user/assistant 消息，限制条数与单条长度
    cleaned = []
    for m in messages[-20:]:
        role = m.get('role')
        content = str(m.get('content') or '').strip()
        if role not in ('user', 'assistant') or not content:
            continue
        cleaned.append({'role': role, 'content': content[:4000]})
    if not cleaned:
        return api_error('对话消息为空', 400)

    db = get_db()
    row = db.execute(
        """
        SELECT s.sid, s.pid, s.qid, s.user_answer, s.score, s.dimension_scores,
               s.ai_feedback, s.hit_points, s.missing_points, s.improving_suggestions,
               s.aggregate_json, s.created_at,
               p.title, p.questions, p.answer_keys, p.material
        FROM submissions s
        LEFT JOIN papers p ON s.pid = p.pid
        WHERE s.sid = ? AND s.uid = ?
        """,
        (sid, current_user['uid']),
    ).fetchone()
    if not row:
        return api_error('批改记录不存在或无权访问', 404)

    # 找到题目
    question = {}
    questions = _loads(row['questions'], [])
    if isinstance(questions, list):
        for q in questions:
            if q.get('qid') == row['qid']:
                question = q
                break

    # 本题给定材料（小作文/综合分析等材料类题型需要，便于模型结合材料讲解）
    material = _loads(row['material'], [])
    material = material if isinstance(material, list) else []

    answer_keys = _loads(row['answer_keys'], {})
    record = dict(row)
    context = _build_context(record, question, answer_keys, material=material)

    system_prompt = SYSTEM_TEMPLATE.format(context=context)
    model_cfg = _chat_model_config()
    full_messages = [{'role': 'system', 'content': system_prompt}] + cleaned

    def generate():
        # SSE 协议：先发一个开始事件，前端用于初始化气泡
        yield 'data: {"type":"start"}\n\n'
        buf = []
        started = time.perf_counter()
        for piece in _stream_chat(model_cfg, full_messages):
            buf.append(piece)
            # 转义换行为 \\n，保证单行 JSON
            payload = json.dumps({'type': 'delta', 'content': piece}, ensure_ascii=False)
            yield f'data: {payload}\n\n'
        # 完成事件
        done = json.dumps({
            'type': 'done',
            'latency_ms': int((time.perf_counter() - started) * 1000),
        }, ensure_ascii=False)
        yield f'data: {done}\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )
