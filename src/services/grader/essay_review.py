"""大作文两阶段批改。

阶段一（审题锚点）：只看材料+题目，独立分析命题意图，结果按题目缓存。
阶段二（评分）：拿审题锚点对照考生作文，定档+逐段分析+整体评价。

两阶段的意义：让"这题该写什么"成为客观锚点，避免模型边读作文边猜材料，
导致立意判断被学生答案带偏。锚点与考生无关，因此同一道题只生成一次并缓存。
"""
import hashlib
import json
import logging

from src.api.utils import get_db
from src.services.grader.prompts import (
    build_zuowen_analyze_prompt,
    build_zuowen_grade_prompt,
)
from src.services.grader.provider_adapters import adapter_for_protocol
from src.services.grader.scorer import _parse_provider_json
from src.services.grader.types import ProviderError

logger = logging.getLogger(__name__)


def _anchor_key(pid: str, qid: str, question: dict, material, model_id: str) -> str:
    """锚点缓存键：题目内容 + 模型 id 共同决定。

    审题锚点按模型隔离——A 模型生成的锚点只给 A 用，
    避免弱模型生成的浅层锚点拖累强模型。
    """
    basis = json.dumps(
        {
            "pid": pid,
            "qid": qid,
            "stem": question.get("stem", ""),
            "material_theme": question.get("material_theme", ""),
            "word_limit": question.get("word_limit", ""),
            "material": material or [],
            "model_id": model_id or "",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _load_cached_anchor(anchor_key: str, model_id: str) -> dict | None:
    db = get_db()
    row = db.execute(
        "SELECT anchor_json FROM essay_anchors WHERE anchor_key = ? AND model_id = ?",
        (anchor_key, model_id or ""),
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["anchor_json"])
    except (ValueError, TypeError):
        return None


def _save_anchor(anchor_key: str, model_id: str, pid: str, qid: str, anchor: dict) -> None:
    db = get_db()
    db.execute(
        """INSERT INTO essay_anchors (anchor_key, model_id, pid, qid, anchor_json, updated_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(anchor_key, model_id) DO UPDATE SET
               anchor_json=excluded.anchor_json,
               updated_at=datetime('now')""",
        (anchor_key, model_id or "", pid, qid, json.dumps(anchor, ensure_ascii=False)),
    )
    db.commit()


def _model_call(model_config: dict, messages: list) -> tuple[dict, int]:
    """调用模型并解析 JSON，返回 (payload, latency_ms)。"""
    adapter = adapter_for_protocol(model_config.get("protocol"))
    runtime = dict(model_config)
    runtime["max_attempts"] = 1
    response = adapter.complete(messages, runtime)
    payload = _parse_provider_json(response.content)
    if not isinstance(payload, dict):
        raise ProviderError("response_format", "模型返回的不是 JSON 对象")
    return payload, response.latency_ms


def get_or_create_anchor(
    model_config: dict, question: dict, material, pid: str = "", qid: str = ""
) -> tuple[dict, bool]:
    """获取审题锚点：优先缓存，未命中则调用阶段一模型生成并缓存。

    锚点按模型隔离（A 模型生成的只给 A 用），因此缓存键包含 model_id。
    返回 (anchor, from_cache)。
    """
    model_id = str(model_config.get("model_id") or model_config.get("name") or "")
    anchor_key = _anchor_key(pid, qid, question, material, model_id)
    cached = _load_cached_anchor(anchor_key, model_id)
    if cached:
        return cached, True

    messages = build_zuowen_analyze_prompt(question, material)
    anchor, _ = _model_call(model_config, messages)

    # 锚点最小健全性校验
    if not anchor.get("core_topic") and not anchor.get("intended_theses"):
        raise ProviderError("response_format", "审题锚点缺少核心字段")

    try:
        _save_anchor(anchor_key, model_id, pid, qid, anchor)
    except Exception:
        # 缓存失败不应中断批改
        logger.warning("保存审题锚点缓存失败", exc_info=True)
    return anchor, False


def grade_essay_two_stage(
    model_config: dict,
    question: dict,
    user_answer: str,
    material,
    pid: str = "",
    qid: str = "",
) -> dict:
    """大作文两阶段批改主入口。

    返回与旧版 grade_with_model 兼容的 dict（score_rate/dimension_scores/
    hit_points/missing_points/ai_feedback/improving_suggestions），
    额外携带两阶段特有字段（tier/anchor/paragraph_analysis 等）。
    """
    # 阶段一：审题锚点（带缓存）
    anchor, anchor_from_cache = get_or_create_anchor(model_config, question, material, pid, qid)

    # 阶段二：拿锚点评分
    messages = build_zuowen_grade_prompt(question, user_answer, anchor)
    payload, latency_ms = _model_call(model_config, messages)

    # 维度分归一
    dims = payload.get("dimension_scores") or {}

    result = {
        "score_rate": payload.get("score_rate"),
        "tier": payload.get("tier", ""),
        "tier_score_range": payload.get("tier_score_range", ""),
        "tier_reason": payload.get("tier_reason", ""),
        "genre_judgment": payload.get("genre_judgment", {}),
        "thesis_comparison": payload.get("thesis_comparison", {}),
        "paragraph_analysis": payload.get("paragraph_analysis", []),
        "dimension_scores": dims,
        "structure_analysis": payload.get("structure_analysis", {}),
        "bonus_points": payload.get("bonus_points", []),
        "penalty_points": payload.get("penalty_points", []),
        "overall_evaluation": payload.get("overall_evaluation", ""),
        "top_improvements": payload.get("top_improvements", []),
        "ai_feedback": payload.get("overall_evaluation") or payload.get("tier_reason", ""),
        "improving_suggestions": payload.get("top_improvements", []),
        # 大作文是主观题，无采分点命中
        "hit_points": [],
        "missing_points": [],
        # 元信息
        "anchor": anchor,
        "anchor_from_cache": anchor_from_cache,
        "latency_ms": latency_ms,
        "model_id": str(model_config.get("model_id") or ""),
    }
    return result