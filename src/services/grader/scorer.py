# AI 批改核心模块
#
# 负责调用 LLM、融合本地规则、生成最终批改结果
# 支持分题型评分和旧版通用评分两种模式

import json
import logging
import math
from src.services.grader.cache import grader_cache
from src.services.grader.prompts import build_grading_prompt, build_simple_feedback_prompt
from src.services.grader.dimensions import (
    calculate_dimensions,
    calculate_type_dimensions,
    calculate_total_score,
    detect_colloquial,
    detect_formal_language,
    detect_generic_countermeasures,
    detect_logic_chain,
    check_document_format,
    check_countermeasure_quality,
    count_chinese_chars,
)
from src.config import get_llm_config
from src.services.grader.point_ids import normalize_key_points
from src.services.grader.rubric import normalize_question_type
from src.services.grader.llm_client import call_chat_completion
from src.services.grader.provider_adapters import adapter_for_protocol
from src.services.grader.types import JudgeResult, ProviderError
from src.services.model_registry import (
    DEFAULT_MODEL_TIMEOUT_SECONDS,
    MAX_MODEL_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

# 分题型维度满分映射
TYPE_DIMENSION_MAX = {
    "guina": {
        "point_coverage": 70, "conciseness": 15, "accuracy": 10, "format": 5
    },
    "zonghe": {
        "logic_chain": 30, "point_coverage": 30, "depth": 20, "language": 10, "format": 10
    },
    "duice": {
        "problem_identification": 20, "targeting": 25, "feasibility": 25,
        "specificity": 20, "format": 10
    },
    "zhixing": {
        "format_correctness": 20, "purpose_achievement": 25,
        "content_completeness": 30, "language_appropriateness": 15, "word_count": 10
    },
    "zuowen": {
        "thesis_accuracy": 25, "argument_richness": 25, "structure": 20,
        "language": 20, "innovation": 10
    }
}


def call_llm(messages: list, parse_json: bool = True, feature_key: str | None = None):
    """Call the currently configured model through the validated client.

    当指定 feature_key 时，优先使用该功能绑定的模型（回退系统全局配置）；
    否则直接使用系统全局配置。
    """
    if feature_key:
        from src.services.feature_model_service import call_feature_llm
        return call_feature_llm(feature_key, messages, parse_json=parse_json)
    return call_chat_completion(messages, get_llm_config(), parse_json=parse_json)


def _repair_unescaped_quotes(text: str) -> str:
    """修复 JSON 字符串值内的未转义双引号。

    reasoning 模型（claude/step）常在 ai_feedback 等长文本字段里用未转义的
    英文双引号包裹引文，破坏 JSON 结构。状态机扫描：处于字符串内部时，若遇到的
    双引号后面（跳过空白）不是结构字符（,:}]）或文本结尾，则判定为值内裸引号并转义。
    """
    out = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue
        if ch == '\\':
            out.append(ch)
            if i + 1 < n:
                out.append(text[i + 1])
                i += 2
            else:
                i += 1
            continue
        if ch == '"':
            j = i + 1
            while j < n and text[j] in ' \t\r\n':
                j += 1
            if j >= n or text[j] in ',:}]':
                out.append('"')
                in_string = False
            else:
                out.append('\\"')
            i += 1
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def _try_load_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _parse_provider_json(content: str) -> dict:
    import re

    cleaned = (content or '').strip()
    if cleaned.startswith('```json'):
        cleaned = cleaned[7:]
    elif cleaned.startswith('```'):
        cleaned = cleaned[3:]
    if cleaned.endswith('```'):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    # 1) direct parse
    payload = _try_load_json(cleaned)
    if isinstance(payload, dict):
        return payload

    # 2) extract first {...} block (tolerate trailing prose)
    match = re.search(r'\{[\s\S]*\}', cleaned)
    candidate = match.group(0) if match else cleaned

    # 3) repair unescaped quotes inside string values (claude/step reasoning models)
    for text in (candidate, _repair_unescaped_quotes(candidate)):
        payload = _try_load_json(text)
        if isinstance(payload, dict):
            return payload

    raise ProviderError('response_format', 'Invalid model response')


def _point_results(value, missing: bool = False) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    results = []
    for index, point in enumerate(value, 1):
        if isinstance(point, str):
            point = {'point': point, 'score': 0 if missing else 1, 'max_score': 1}
        if not isinstance(point, dict):
            continue
        item = dict(point)
        point_id = item.get('point_id')
        if not isinstance(point_id, str) or not point_id.strip():
            # Models often omit IDs; assign temporary ones and remap later.
            point_id = f'auto_{index}'
        if missing:
            item.setdefault('score', 0)
            item.setdefault('verdict', 'none')
        else:
            item.setdefault('verdict', 'full' if item.get('score') else 'partial')
        try:
            score = float(item.get('score') or 0)
            max_score = float(item.get('max_score') if item.get('max_score') is not None else max(score, 1))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(score) or not math.isfinite(max_score) or max_score < 0:
            continue
        score = max(0.0, min(score, max_score if max_score > 0 else score))
        verdict = str(item.get('verdict') or 'partial').lower()
        if verdict not in {'full', 'partial', 'none'}:
            verdict = 'none' if score <= 0 else ('full' if max_score and score >= max_score else 'partial')
        item['point_id'] = point_id.strip()
        item['score'] = float(score)
        item['max_score'] = float(max_score if max_score > 0 else 1)
        item['verdict'] = verdict
        item.setdefault('point', str(item.get('point') or item.get('description') or item['point_id']))
        item.setdefault('evidence', str(item.get('evidence') or ''))
        results.append(item)
    return results


def _normalize_point_set(
    question: dict,
    hit_points: list[dict],
    missing_points: list[dict],
) -> tuple[list[dict], list[dict]]:
    expected = {}
    expected_list = []
    for point in normalize_key_points(question):
        point_id = point.get('point_id')
        if not isinstance(point_id, str) or not point_id.strip():
            continue
        raw_max_score = point.get('max_score', point.get('score'))
        try:
            max_score = float(raw_max_score)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(max_score) or max_score <= 0:
            continue
        item = {
            'point_id': point_id.strip(),
            'point': str(point.get('point') or point_id).strip(),
            'max_score': max_score,
        }
        expected[item['point_id']] = item
        expected_list.append(item)

    if not expected_list:
        # No rubric points configured — keep model output as soft feedback only.
        return hit_points or [], missing_points or []

    def _match_expected(point: dict) -> dict | None:
        pid = str(point.get('point_id') or '').strip()
        if pid in expected:
            return expected[pid]
        text = str(point.get('point') or '').strip()
        if text:
            for item in expected_list:
                if item['point'] == text or text in item['point'] or item['point'] in text:
                    return item
        return None

    normalized_hits = []
    normalized_missing = []
    seen = set()

    for point in hit_points or []:
        rubric = _match_expected(point)
        if not rubric or rubric['point_id'] in seen:
            continue
        score = float(point.get('score') or 0)
        if score <= 0:
            continue
        score = min(score, rubric['max_score'])
        verdict = point.get('verdict') if point.get('verdict') in {'full', 'partial'} else (
            'full' if score >= rubric['max_score'] else 'partial'
        )
        normalized_hits.append({
            'point_id': rubric['point_id'],
            'point': rubric['point'],
            'score': score,
            'max_score': rubric['max_score'],
            'evidence': str(point.get('evidence') or ''),
            'verdict': verdict,
        })
        seen.add(rubric['point_id'])

    for point in missing_points or []:
        rubric = _match_expected(point)
        if not rubric or rubric['point_id'] in seen:
            continue
        normalized_missing.append({
            'point_id': rubric['point_id'],
            'point': rubric['point'],
            'score': 0.0,
            'max_score': rubric['max_score'],
            'evidence': '',
            'verdict': 'none',
        })
        seen.add(rubric['point_id'])

    for rubric in expected_list:
        if rubric['point_id'] in seen:
            continue
        normalized_missing.append({
            'point_id': rubric['point_id'],
            'point': rubric['point'],
            'score': 0.0,
            'max_score': rubric['max_score'],
            'evidence': '',
            'verdict': 'none',
        })
    return normalized_hits, normalized_missing


def _score_rate_from_payload(payload: dict, question: dict) -> float:
    explicit_rate = payload.get('score_rate')
    if isinstance(explicit_rate, (int, float)) and not isinstance(
        explicit_rate, bool
    ):
        return max(0.0, min(float(explicit_rate), 100.0))

    legacy_score = payload.get('score')
    if not isinstance(legacy_score, (int, float)) or isinstance(
        legacy_score, bool
    ):
        raise ProviderError('response_format', 'Invalid model response')

    question_type = normalize_question_type(
        question.get('type'), question.get('stem', '')
    )
    try:
        score_max = float(question.get('score_max', 100))
    except (TypeError, ValueError):
        score_max = 100.0
    if question_type == 'zuowen' and score_max == 40:
        legacy_score = float(legacy_score) * 100 / score_max
    return max(0.0, min(float(legacy_score), 100.0))


def grade_with_model(
    model_config: dict,
    question: dict,
    user_answer: str,
    material: list | None,
    sid: str | None = None,
) -> JudgeResult:
    """Grade one answer through a registered provider model."""
    # 大作文走两阶段批改（先独立审题生成锚点，再对照评分+逐段分析）
    qtype = normalize_question_type(question.get('type'), question.get('stem', ''))
    if qtype == 'zuowen':
        from src.services.grader.essay_review import grade_essay_two_stage
        essay = grade_essay_two_stage(
            model_config, question, user_answer, material,
            pid=str(question.get('pid') or question.get('paper_id') or ''),
            qid=str(question.get('qid') or question.get('id') or ''),
        )
        return JudgeResult(
            model_id=str(model_config.get('model_id') or ''),
            score_rate=essay.get('score_rate'),
            dimension_scores=essay.get('dimension_scores') or {},
            hit_points=essay.get('hit_points') or [],
            missing_points=essay.get('missing_points') or [],
            ai_feedback=str(essay.get('ai_feedback') or ''),
            improving_suggestions=essay.get('improving_suggestions') or [],
            # 两阶段特有字段透传到 aggregate 层
            essay_anchor=essay.get('anchor'),
            tier=essay.get('tier', ''),
            tier_reason=essay.get('tier_reason', ''),
            genre_judgment=essay.get('genre_judgment', {}),
            thesis_comparison=essay.get('thesis_comparison', {}),
            paragraph_analysis=essay.get('paragraph_analysis', []),
            structure_analysis=essay.get('structure_analysis', {}),
            overall_evaluation=essay.get('overall_evaluation', ''),
            top_improvements=essay.get('top_improvements', []),
            anchor_from_cache=bool(essay.get('anchor_from_cache')),
            raw_metadata={'latency_ms': essay.get('latency_ms'), 'two_stage': True},
            latency_ms=essay.get('latency_ms'),
        )

    messages = build_grading_prompt(question, user_answer, material)
    adapter = adapter_for_protocol(model_config.get('protocol'))
    runtime_config = dict(model_config)
    try:
        requested_timeout = int(
            runtime_config.get(
                'timeout_seconds', DEFAULT_MODEL_TIMEOUT_SECONDS
            )
        )
    except (TypeError, ValueError):
        raise ProviderError('configuration', 'Invalid model timeout') from None
    runtime_config['timeout_seconds'] = max(
        5, min(requested_timeout, MAX_MODEL_TIMEOUT_SECONDS)
    )
    runtime_config['max_attempts'] = 1
    # Single adapter attempt: a slow/hanging model fails within one timeout
    # (<=180s) instead of 3x180=540s, which exceeds the gunicorn worker timeout
    # (300s) and SIGKILLs the whole grading request (zero judgments persisted).
    # JSON format retries once in the loop below (down from 3); a transient network
    # blip costs one failed judge in ensemble mode (other models still complete).
    payload = None
    last_error = None
    response = None
    for _attempt in range(2):
        try:
            response = adapter.complete(messages, runtime_config)
            # 记录 token 消耗（从响应 usage 提取，按模型价格计算成本）
            try:
                from src.services import token_usage_service
                meta = response.raw_metadata or {}
                token_usage_service.record_usage(
                    model_id=str(model_config.get('model_id') or ''),
                    model_name=str(model_config.get('model_name') or model_config.get('name') or ''),
                    prompt_tokens=int(meta.get('prompt_tokens') or 0),
                    completion_tokens=int(meta.get('completion_tokens') or 0),
                    source='grading',
                    sid=sid,
                )
            except Exception:
                logger.warning("记录 token 消耗失败", exc_info=True)
            payload = _parse_provider_json(response.content)
            break
        except ProviderError as exc:
            if exc.code == 'response_format' and _attempt < 1:
                last_error = exc
                continue
            raise
    if payload is None:
        raise last_error

    raw_dimensions = payload.get('dimension_scores', {})
    if not isinstance(raw_dimensions, dict):
        raw_dimensions = {}
    dimensions = {}
    for name, value in raw_dimensions.items():
        try:
            if isinstance(value, bool):
                continue
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            dimensions[str(name)] = number
    suggestions = payload.get('improving_suggestions', [])
    if not isinstance(suggestions, list):
        suggestions = [str(suggestions)] if suggestions else []
    hit_points, missing_points = _normalize_point_set(
        question,
        _point_results(payload.get('hit_points')),
        _point_results(payload.get('missing_points'), missing=True),
    )

    # Prefer rubric-derived score to stop inflated model self-scores.
    rubric_points = normalize_key_points(question)
    score_rate = _score_rate_from_payload(payload, question)
    # Skip rubric-derived capping for sparse/placeholder rubrics (<=1 point):
    # custom questions without real key_points get a single vague placeholder
    # point; the model often marks it 0-hit -> derived=0 -> score capped to
    # ~3%, ignoring the model's own (reasonable) score_rate. Trust the model
    # self-report when the rubric is too sparse to be reliable.
    if rubric_points and len(rubric_points) >= 2:
        total_max = 0.0
        total_got = 0.0
        for point in hit_points:
            total_got += float(point.get('score') or 0)
            total_max += float(point.get('max_score') or 0)
        for point in missing_points:
            total_max += float(point.get('max_score') or 0)
        if total_max > 0:
            derived = max(0.0, min(100.0, total_got / total_max * 100.0))
            # Balanced scoring: trust model when rubric coverage is high,
            # cap model when rubric coverage is low.
            if derived >= 90:
                # High coverage: trust the model's score_rate, don't cap down
                score_rate = max(float(score_rate), derived)
            elif derived >= 60:
                # Moderate coverage: use the higher of model and derived
                score_rate = max(float(score_rate), derived - 5.0)
                score_rate = min(float(score_rate), 100.0)
            else:
                # Low coverage: cap model score to prevent inflation
                score_rate = min(float(score_rate), derived + 5.0)
            score_rate = round(score_rate, 2)

    return JudgeResult(
        model_id=str(model_config.get('model_id') or ''),
        score_rate=score_rate,
        dimension_scores=dimensions,
        hit_points=hit_points,
        missing_points=missing_points,
        ai_feedback=str(payload.get('ai_feedback') or ''),
        improving_suggestions=suggestions,
        raw_metadata=dict(response.raw_metadata or {}),
        latency_ms=response.latency_ms,
    )


def _clamp(value, min_val, max_val):
    """将值限制在指定范围内"""
    return max(min_val, min(max_val, value))


def _normalize_llm_scores(llm_scores: dict, dimension_max: dict) -> dict:
    """校验并修正 LLM 返回的维度分数，确保不超出满分范围

    Args:
        llm_scores: LLM 返回的原始维度分数
        dimension_max: 各维度满分

    Returns:
        修正后的维度分数
    """
    normalized = {}
    for dim, max_val in dimension_max.items():
        raw = llm_scores.get(dim, 0)
        if isinstance(raw, (int, float)):
            normalized[dim] = _clamp(int(raw), 0, max_val)
        else:
            normalized[dim] = 0
    return normalized


def grade_answer(pid: str, qid: str, question: dict,
                 user_answer: str, material: list = None) -> dict:
    """批改答案（新版分题型评分）

    流程：
    1. 检查缓存
    2. 根据题型选择 Prompt
    3. 调用 LLM 获取评分
    4. 本地规则校验
    5. 融合分数
    6. 缓存并返回结果

    Args:
        pid: 试卷 ID
        qid: 题目 ID
        question: 题目信息字典
        user_answer: 考生答案
        material: 给定材料（可选）

    Returns:
        批改结果字典
    """
    # 1. 检查缓存
    cached = grader_cache.get(pid, qid, user_answer)
    if cached:
        cached['from_cache'] = True
        return cached

    question_type = normalize_question_type(
        question.get('type'), question.get('stem', '')
    )

    # 2. 构建 Prompt 并调用 LLM
    messages = build_grading_prompt(question, user_answer, material)

    try:
        llm_result = call_llm(messages, feature_key="grading")
    except Exception as e:
        logger.error(f"批改失败 (pid={pid}, qid={qid}): {e}")
        raise Exception(f"批改失败: {str(e)}")

    # 3. 提取 LLM 返回的维度分数
    llm_raw_scores = llm_result.get('dimension_scores', {})
    dimension_max = TYPE_DIMENSION_MAX.get(question_type, TYPE_DIMENSION_MAX['guina'])
    llm_scores = _normalize_llm_scores(llm_raw_scores, dimension_max)

    # 4. 本地规则校验
    local_checks = {}
    local_checks['char_count'] = count_chinese_chars(user_answer)
    local_checks['colloquial'] = detect_colloquial(user_answer)
    local_checks['formal_phrases'] = detect_formal_language(user_answer)

    if question_type == 'zhixing':
        doc_type = question.get('document_type', '讲话稿')
        local_checks['format_check'] = check_document_format(user_answer, doc_type)

    if question_type == 'duice':
        local_checks['generic_countermeasures'] = detect_generic_countermeasures(user_answer)

    if question_type == 'zonghe':
        local_checks['logic_chain'] = detect_logic_chain(user_answer)

    # 5. 融合分数（以 LLM 为主，本地规则做修正）
    merged_scores = dict(llm_scores)

    # 语言维度：扣除本地检测到的口语化扣分
    lang_dim = 'language' if question_type in ('zonghe', 'zuowen') else 'language_appropriateness'
    if lang_dim in merged_scores:
        colloquial_penalty = len(local_checks['colloquial']) * 2
        max_lang = dimension_max.get(lang_dim, 20)
        merged_scores[lang_dim] = _clamp(
            merged_scores[lang_dim] - colloquial_penalty, 0, max_lang
        )

    # 贯彻执行格式：融合本地检测结果
    if question_type == 'zhixing' and 'format_correctness' in merged_scores:
        fmt = local_checks.get('format_check', {})
        local_fmt_ratio = fmt.get('score_ratio', 1.0)
        llm_fmt = merged_scores['format_correctness']
        # 取本地和 LLM 的平均值
        merged_scores['format_correctness'] = int(
            (llm_fmt + 20 * local_fmt_ratio) / 2
        )

    # 6. 计算总分
    llm_score = _score_rate_from_payload(llm_result, question)
    type_score = calculate_total_score(question_type, merged_scores)

    # 评分融合：以 LLM 总分为主，维度分做参考修正
    if abs(llm_score - type_score) > 15:
        # 差距大时加权平均：LLM 权重 70%，维度分 30%
        final_score = round(llm_score * 0.7 + type_score * 0.3, 1)
    else:
        # 差距小时以 LLM 总分为准
        final_score = _clamp(llm_score, 0, 100)

    # 7. 构建最终结果
    result = {
        'score': final_score,
        'dimension_scores': merged_scores,
        'hit_points': llm_result.get('hit_points', []),
        'missing_points': llm_result.get('missing_points', []),
        'ai_feedback': llm_result.get('ai_feedback', ''),
        'improving_suggestions': llm_result.get('improving_suggestions', []),
        'question_type': question_type,
        'local_checks': local_checks,
        'from_cache': False,
    }

    # 题型特有字段
    if question_type == 'zonghe':
        result['logic_chain_analysis'] = llm_result.get('logic_chain_analysis', {})
    if question_type == 'duice':
        result['countermeasures'] = llm_result.get('countermeasures', [])
        result['generic_countermeasures'] = llm_result.get('generic_countermeasures', [])
        result['problem_accuracy'] = llm_result.get('problem_accuracy', '')
    if question_type == 'zhixing':
        result['format_check'] = llm_result.get('format_check', {})
        result['content_check'] = llm_result.get('content_check', {})
    if question_type == 'zuowen':
        result['tier'] = llm_result.get('tier', '')
        result['tier_reason'] = llm_result.get('tier_reason', '')
        result['thesis_analysis'] = llm_result.get('thesis_analysis', {})
        result['argument_analysis'] = llm_result.get('argument_analysis', {})
        result['structure_analysis'] = llm_result.get('structure_analysis', {})
        result['bonus_points'] = llm_result.get('bonus_points', [])
        result['penalty_points'] = llm_result.get('penalty_points', [])

    # 8. 缓存结果
    grader_cache.set(pid, qid, user_answer, result)

    return result


def grade_answer_legacy(pid: str, qid: str, question: dict,
                        user_answer: str, material: list = None) -> dict:
    """批改答案（旧版通用评分，保持向后兼容）

    使用旧版的五维度评分体系，不区分题型。
    """
    cached = grader_cache.get(pid, qid, user_answer)
    if cached:
        cached['from_cache'] = True
        return cached

    messages = build_grading_prompt(question, user_answer, material)

    try:
        result = call_llm(messages, feature_key="grading")
        result['from_cache'] = False

        # 使用旧版维度计算做校验
        hit_points = result.get('hit_points', [])
        missing_points = result.get('missing_points', [])
        local_dims = calculate_dimensions(question, user_answer, hit_points, missing_points)

        # 将 LLM 返回的中文维度名映射到本地
        llm_dims = result.get('dimension_scores', {})
        dim_mapping = {
            '踩点命中': 'point_coverage',
            '逻辑结构': 'logic_structure',
            '语言规范': 'language',
            '字数控制': 'word_count',
            '卷面整洁': 'format'
        }

        # 如果 LLM 返回的是中文维度名，转换为英文
        if any(k in dim_mapping for k in llm_dims):
            converted = {}
            for cn_key, en_key in dim_mapping.items():
                if cn_key in llm_dims:
                    converted[en_key] = llm_dims[cn_key]
            result['dimension_scores'] = converted

        grader_cache.set(pid, qid, user_answer, result)
        return result

    except Exception as e:
        raise Exception(f"批改失败: {str(e)}")


def get_simple_feedback(question: dict, user_answer: str) -> str:
    """获取简化反馈（免费用户）

    Args:
        question: 题目信息
        user_answer: 考生答案

    Returns:
        简要评价文本
    """
    prompt = build_simple_feedback_prompt(question, user_answer)
    try:
        messages = [
            {"role": "system", "content": "你是一位申论老师，请简要评价学生答案。"},
            {"role": "user", "content": prompt}
        ]
        return call_llm(messages, parse_json=False, feature_key="simple_feedback")
    except Exception as e:
        logger.error(f"简化反馈生成失败: {e}")
        return "答案已提交。由于服务繁忙，详细批改将在稍后完成。"
