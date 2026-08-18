"""Create ephemeral custom papers for user-uploaded material grading."""

from __future__ import annotations

import json
import re
from datetime import datetime

from src.api.utils import generate_uuid, get_db
from src.services.grader.rubric import normalize_question_type


TYPE_LABELS = {
    'guina': '归纳概括',
    'zonghe': '综合分析',
    'duice': '提出对策',
    'zhixing': '贯彻执行',
    'zuowen': '大作文',
}


def _split_material(material) -> list[str]:
    if material is None:
        return []
    if isinstance(material, list):
        parts = [str(item).strip() for item in material if str(item).strip()]
        return parts
    text = str(material).strip()
    if not text:
        return []
    # Prefer blank-line paragraphs, then numbered material markers.
    chunks = [part.strip() for part in re.split(r'\n\s*\n+', text) if part.strip()]
    if len(chunks) <= 1:
        chunks = [
            part.strip()
            for part in re.split(r'(?=资料[0-9一二三四五六七八九十]+|材料[0-9一二三四五六七八九十]+)', text)
            if part.strip()
        ]
    return chunks or [text]


def _parse_key_points(raw_points, score_max: float) -> list[dict]:
    if not raw_points:
        return []
    if isinstance(raw_points, str):
        text = raw_points.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            return _parse_key_points(parsed, score_max)
        except (json.JSONDecodeError, TypeError):
            lines = [line.strip(' -\t') for line in re.split(r'[\n;；]+', text) if line.strip()]
            if not lines:
                return []
            each = max(1.0, round(float(score_max) / len(lines), 1))
            return [
                {
                    'point': line,
                    'score': each if i < len(lines) - 1 else max(1.0, float(score_max) - each * (len(lines) - 1)),
                    'alias': [],
                }
                for i, line in enumerate(lines)
            ]
    if isinstance(raw_points, list):
        points = []
        for item in raw_points:
            if isinstance(item, str) and item.strip():
                points.append({'point': item.strip(), 'score': 0, 'alias': []})
            elif isinstance(item, dict) and (item.get('point') or item.get('content')):
                point = {
                    'point': str(item.get('point') or item.get('content')).strip(),
                    'score': float(item.get('score') or item.get('max_score') or 0),
                    'alias': item.get('alias') or [],
                }
                if item.get('point_id'):
                    point['point_id'] = item['point_id']
                points.append(point)
        if points and all(float(p.get('score') or 0) <= 0 for p in points):
            each = max(1.0, round(float(score_max) / len(points), 1))
            for i, point in enumerate(points):
                point['score'] = (
                    each if i < len(points) - 1
                    else max(1.0, float(score_max) - each * (len(points) - 1))
                )
        return points
    return []


def _extract_points_from_reference(reference: str, score_max: float, stem: str = "") -> list[dict]:
    """Use LLM to extract structured key_points from a reference answer text."""
    import json as _json
    from src.config import get_llm_config
    from src.services.grader.llm_client import call_chat_completion

    messages = [
        {"role": "system", "content": """你是申论阅卷专家。你的任务是从参考答案中提取结构化的采分点。
每个采分点是一个独立的给分项，需要包含：
- point: 采分点的完整描述（保留原文关键表述）
- score: 该采分点的建议分值
- alias: 同义表达列表（考生可能用哪些不同的表述来表达同一个意思）

原则：
1. 根据参考答案的自然分段或要点序号来拆解
2. 每个采分点应当是独立的、可单独判定的给分项
3. 所有采分点的分值之和应大约等于总分
4. 采分点数量通常在3-8个之间
5. 不要漏掉参考答案中的任何要点

请只输出合法JSON数组，不要markdown标记。"""},
        {"role": "user", "content": f"""题目：{stem or '申论题目'}

满分：{score_max}分

参考答案：
{reference[:4000]}

请从以上参考答案中提取采分点，按以下JSON数组格式输出：
[
  {{"point": "采分点完整描述", "score": 分值, "alias": ["同义表达1", "同义表达2"]}},
  ...
]"""}
    ]

    from src.services.feature_model_service import call_feature_llm
    result_text = call_feature_llm("reference_extract", messages, parse_json=False)
    result_text = (result_text or "").strip()
    if result_text.startswith("```"):
        lines = result_text.split("\n")
        result_text = "\n".join(lines[1:]) if lines[0].startswith("```") else result_text
    if result_text.endswith("```"):
        result_text = result_text[:-3]
    result_text = result_text.strip()

    points = _json.loads(result_text)
    if not isinstance(points, list):
        return []

    raw_total = sum(float(p.get("score", 0)) for p in points if isinstance(p, dict))
    scale = score_max / raw_total if raw_total > 0 else score_max / max(len(points), 1)

    normalized = []
    for p in points:
        if not isinstance(p, dict):
            continue
        point_text = str(p.get("point", "")).strip()
        if not point_text:
            continue
        normalized.append({
            "point": point_text,
            "score": round(float(p.get("score", 0)) * scale, 1),
            "alias": p.get("alias", []) if isinstance(p.get("alias"), list) else [],
        })

    if normalized:
        current_total = sum(pt["score"] for pt in normalized)
        if current_total > 0:
            normalized[-1]["score"] = round(normalized[-1]["score"] + score_max - current_total, 1)

    return normalized


def create_custom_paper(
    uid: str,
    material,
    stem: str,
    user_answer: str,
    reference_answer: str = '',
    question_type: str = 'guina',
    score_max: float = 20,
    word_limit: str = '',
    key_points=None,
    title: str = '',
) -> dict:
    """Persist a private custom paper+question and return grading payload."""
    del user_answer  # reserved for future answer-side validation / snapshots
    material_parts = _split_material(material)
    if not material_parts:
        raise ValueError('材料不能为空')
    stem = (stem or '').strip()
    if not stem:
        raise ValueError('题目不能为空')

    qtype = normalize_question_type(question_type, stem)
    try:
        score_max = float(score_max or 20)
    except (TypeError, ValueError):
        score_max = 20.0
    score_max = max(1.0, min(score_max, 100.0))

    points = _parse_key_points(key_points, score_max)
    reference_answer = (reference_answer or '').strip()
    if not points and reference_answer:
        # Use LLM to extract structured key_points from reference answer
        try:
            points = _extract_points_from_reference(reference_answer, score_max, stem)
        except Exception:
            # Fallback: split reference answer into lines as points
            points = _parse_key_points(reference_answer, score_max)
        # If still empty, provide a minimal placeholder
        if not points:
            points = [{
                'point': '对照参考答案覆盖主要采分点',
                'score': score_max,
                'alias': [],
            }]

    pid = 'custom_' + generate_uuid().replace('-', '')[:16]
    qid = 'q1'
    paper_title = (title or '').strip() or f'自定义批改 {datetime.now().strftime("%Y-%m-%d %H:%M")}'
    question = {
        'qid': qid,
        'type': TYPE_LABELS.get(qtype, qtype),
        'stem': stem,
        'score_max': score_max,
        'word_limit': word_limit or '',
        'key_points': points,
        'reference_answer': reference_answer,
    }
    answer_keys = {qid: reference_answer} if reference_answer else {}

    db = get_db()
    db.execute(
        """INSERT INTO papers
           (pid, source, exam_type, year, season, province, title,
            material, questions, answer_keys, difficulty, heat, tag, status, created_at)
           VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, 3, 0, ?, 'custom', ?)""",
        (
            pid,
            '用户上传',
            '自定义',
            datetime.now().year,
            paper_title,
            json.dumps(material_parts, ensure_ascii=False),
            json.dumps([question], ensure_ascii=False),
            json.dumps(answer_keys, ensure_ascii=False),
            json.dumps(['custom', uid], ensure_ascii=False),
            datetime.now().isoformat(),
        ),
    )
    db.commit()

    runtime_question = dict(question)
    runtime_question['material'] = material_parts
    return {
        'pid': pid,
        'qid': qid,
        'title': paper_title,
        'question': runtime_question,
        'material': material_parts,
    }
