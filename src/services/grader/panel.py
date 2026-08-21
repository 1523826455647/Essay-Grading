"""大作文评审团 + 仲裁（P2）。

在常规两阶段批改之后，对「边界分数 / 体裁存疑 / 用户指定深度批改」的大作文，
并行运行 5 个维度子评审（立意/论证/结构/语言/创新），再用仲裁模型综合定档。

成本与时延控制：
- 5 位子评审用 ThreadPoolExecutor 并行，总时延 ≈ max(子评审) + 仲裁
- 子评审 max_tokens 上限 2000，输出短、更快
- 任意子评审失败（<2 位成功）或仲裁失败，整体降级回常规结果，不影响批改主流程
"""
import logging
from concurrent.futures import ThreadPoolExecutor

from src.services.grader.prompts import (
    build_zuowen_arbitration_prompt,
    build_zuowen_reviewer_prompt,
)
from src.services.grader.provider_adapters import adapter_for_protocol
from src.services.grader.scorer import _parse_provider_json
from src.services.grader.types import ProviderError

logger = logging.getLogger(__name__)

# 评审团五个维度（维度键、中文名）——与评分维度一一对应
PANEL_DIMENSIONS = [
    ("thesis_accuracy", "立意"),
    ("argument_richness", "论证"),
    ("structure", "结构"),
    ("language", "语言"),
    ("innovation", "创新"),
]

# 四档的百分制边界：一类 >=77.5、二类 >=52.5、三类 >=27.5
TIER_CUTOFFS = (77.5, 52.5, 27.5)
BOUNDARY_MARGIN = 5.0

# 子评审单个输出 token 上限（控制单次时延）
REVIEWER_MAX_TOKENS = 2000


def should_run_panel(initial: dict, deep: bool = False) -> tuple[bool, str]:
    """判断是否需要对本次大作文升级为评审团。

    触发条件（满足其一）：
    1. 深度批改：用户显式勾选 deep
    2. 边界分数：score_rate 落在某档位分界线附近
    3. 体裁存疑：常规批改判定文体不符（高影响、值得复核）
    返回 (是否触发, 触发原因)。
    """
    if deep:
        return True, "深度批改"

    score = initial.get("score_rate")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        for cutoff in TIER_CUTOFFS:
            if abs(float(score) - cutoff) <= BOUNDARY_MARGIN:
                return True, f"边界分数（{score:.0f} 分处于分档线附近）"

    genre = (initial.get("genre_judgment") or {}).get("is_correct_genre")
    if genre is False:
        return True, "体裁判定存疑（需复核定档）"
    return False, ""


def _record_usage(model_config: dict, response, sid: str | None) -> None:
    try:
        from src.services import token_usage_service
        meta = response.raw_metadata or {}
        token_usage_service.record_usage(
            model_id=str(model_config.get("model_id") or ""),
            model_name=str(
                model_config.get("model_name") or model_config.get("name") or ""
            ),
            prompt_tokens=int(meta.get("prompt_tokens") or 0),
            completion_tokens=int(meta.get("completion_tokens") or 0),
            source="grading",
            sid=sid,
        )
    except Exception:
        logger.warning("记录评审团 token 消耗失败", exc_info=True)


def _reviewer_call(
    model_config: dict,
    question: dict,
    user_answer: str,
    anchor: dict,
    dimension_key: str,
    dimension_label: str,
    sid: str | None,
) -> dict:
    messages = build_zuowen_reviewer_prompt(
        question, user_answer, anchor, dimension_key, dimension_label
    )
    runtime = dict(model_config)
    runtime["max_attempts"] = 1
    try:
        mt = int(runtime.get("max_tokens") or 0)
    except (TypeError, ValueError):
        mt = 0
    runtime["max_tokens"] = max(256, min(mt if mt > 0 else REVIEWER_MAX_TOKENS, REVIEWER_MAX_TOKENS))
    adapter = adapter_for_protocol(model_config.get("protocol"))
    response = adapter.complete(messages, runtime)
    _record_usage(model_config, response, sid)
    payload = _parse_provider_json(response.content)
    if not isinstance(payload, dict):
        raise ProviderError("response_format", "评审返回的不是 JSON 对象")
    return _normalize_review(payload, dimension_key, dimension_label)


def _normalize_review(payload: dict, dimension_key: str, dimension_label: str) -> dict:
    """规范化单份评审：字段兜底 + 分数/档次一致性约束。"""
    tier = str(payload.get("tier_vote") or "").strip()
    try:
        score = float(payload.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(score, 100.0))
    review = {
        "dimension": dimension_key or payload.get("dimension", ""),
        "dimension_label": dimension_label or payload.get("dimension_label", ""),
        "score": round(score, 1),
        "tier_vote": tier,
        "tier_score_range": str(payload.get("tier_score_range") or ""),
        "confidence": str(payload.get("confidence") or "中"),
        "evidence": str(payload.get("evidence") or ""),
        "strengths": str(payload.get("strengths") or ""),
        "issues": str(payload.get("issues") or ""),
        "suggestion": str(payload.get("suggestion") or ""),
    }
    return review


def _arbitrate(
    model_config: dict,
    question: dict,
    user_answer: str,
    anchor: dict,
    initial: dict,
    reviews: list,
    sid: str | None,
) -> dict:
    messages = build_zuowen_arbitration_prompt(
        question, user_answer, anchor, initial, reviews
    )
    runtime = dict(model_config)
    runtime["max_attempts"] = 1
    adapter = adapter_for_protocol(model_config.get("protocol"))
    response = adapter.complete(messages, runtime)
    _record_usage(model_config, response, sid)
    payload = _parse_provider_json(response.content)
    if not isinstance(payload, dict):
        raise ProviderError("response_format", "仲裁返回的不是 JSON 对象")
    return _normalize_arbitration(payload, reviews, initial)


def _normalize_arbitration(payload: dict, reviews: list, initial: dict) -> dict:
    """规范化仲裁结果；字段缺失时用多数票兜底，保证前端总能渲染。"""
    final_tier = str(payload.get("final_tier") or "").strip()
    if not final_tier:
        # 多数票兜底
        votes = [r.get("tier_vote") for r in reviews if r.get("tier_vote")]
        final_tier = max(set(votes), key=votes.count) if votes else str(initial.get("tier") or "")

    try:
        score_rate = float(payload.get("score_rate"))
    except (TypeError, ValueError):
        # 按档次回填区间中值
        mid = {"一类文": 88.0, "二类文": 65.0, "三类文": 40.0, "四类文": 15.0}.get(
            final_tier, initial.get("score_rate")
        )
        score_rate = float(mid) if mid is not None else 0.0
    score_rate = max(0.0, min(round(score_rate, 1), 100.0))

    dissent = payload.get("dissent_notes") or []
    if not isinstance(dissent, list):
        dissent = []

    return {
        "tier": final_tier,
        "tier_score_range": str(payload.get("tier_score_range") or ""),
        "score_rate": score_rate,
        "tier_reason": str(payload.get("tier_reason") or ""),
        "consensus": str(payload.get("consensus") or "中"),
        "dissent_notes": dissent,
        "overall_evaluation": str(payload.get("overall_evaluation") or ""),
        "top_improvements": payload.get("top_improvements") or [],
    }


def run_review_panel(
    model_config: dict,
    question: dict,
    user_answer: str,
    anchor: dict,
    initial: dict,
    deep: bool = False,
    sid: str | None = None,
) -> dict | None:
    """并行评审团 + 仲裁主入口。

    返回 None 表示不触发或降级（保持常规结果）；否则返回
    {"triggered", "reason", "reviews", "arbitration"}。
    """
    triggered, reason = should_run_panel(initial, deep)
    if not triggered:
        return None

    dimensions = PANEL_DIMENSIONS
    with ThreadPoolExecutor(max_workers=min(5, len(dimensions))) as executor:
        futures = [
            executor.submit(
                _reviewer_call,
                model_config,
                question,
                user_answer,
                anchor,
                dim_key,
                dim_label,
                sid,
            )
            for dim_key, dim_label in dimensions
        ]
        reviews = []
        for future in futures:
            try:
                review = future.result()
            except Exception as exc:
                logger.warning("评审团子评审失败（%s）", exc)
                continue
            if review.get("tier_vote"):
                reviews.append(review)

    if len(reviews) < 2:
        logger.warning("评审团降级：仅 %d/5 位评审成功，回退常规结果", len(reviews))
        return None

    arbitration = None
    try:
        arbitration = _arbitrate(
            model_config, question, user_answer, anchor, initial, reviews, sid
        )
    except Exception as exc:
        logger.warning("评审团仲裁失败，仅保留子评审意见（%s）", exc)

    return {
        "triggered": True,
        "reason": reason,
        "reviews": reviews,
        "arbitration": arbitration,
    }
