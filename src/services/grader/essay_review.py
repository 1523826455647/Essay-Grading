"""大作文智能批改：审题自检 + 两阶段评分 + 评审团仲裁。

阶段一（审题锚点）：只看材料+题目，独立分析命题意图，结果按题目缓存。
P1 自检循环：锚点生成后由「审题复核官」再复核一轮，修订跑偏/遗漏后落缓存。
阶段二（评分）：拿审题锚点对照考生作文，定档+逐段分析+整体评价。
P2 评审团：对边界分数/体裁存疑/深度批改的作文，并行 5 维度子评审 + 仲裁定档。

两阶段的意义：让"这题该写什么"成为客观锚点，避免模型边读作文边猜材料，
导致立意判断被学生答案带偏。锚点与考生无关，因此同一道题只生成一次并缓存。
"""
import hashlib
import json
import logging

from src.api.utils import get_db, get_setting
from src.services.grader.prompts import (
    build_zuowen_analyze_prompt,
    build_zuowen_anchor_check_prompt,
    build_zuowen_grade_prompt,
)
from src.services.grader.provider_adapters import adapter_for_protocol
from src.services.grader.scorer import _parse_provider_json
from src.services.grader.types import ProviderError

logger = logging.getLogger(__name__)

# 审题锚点必含字段（缺省时从原锚点兜底，保证自检修订版结构完整）
_ANCHOR_FIELDS = {
    "core_topic": "",
    "material_position": "",
    "intended_theses": [],
    "offtopic_risks": [],
    "key_concepts": [],
    "intended_genre": "议论文",
    "intended_focus": "",
    "evidence_pool": [],
}


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


def _record_usage(model_config: dict, response, sid: str | None) -> None:
    """记录 token 消耗（大作文两阶段此前未记录，补上以支撑批改消耗统计）。"""
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
        logger.warning("记录审题锚点 token 消耗失败", exc_info=True)


def _model_call(
    model_config: dict,
    messages: list,
    sid: str | None = None,
    max_tokens_cap: int | None = None,
) -> tuple[dict, int]:
    """调用模型并解析 JSON，返回 (payload, latency_ms)。"""
    adapter = adapter_for_protocol(model_config.get("protocol"))
    runtime = dict(model_config)
    runtime["max_attempts"] = 1
    if max_tokens_cap:
        try:
            mt = int(runtime.get("max_tokens") or 0)
        except (TypeError, ValueError):
            mt = 0
        runtime["max_tokens"] = max(
            256, min(mt if mt > 0 else max_tokens_cap, max_tokens_cap)
        )
    response = adapter.complete(messages, runtime)
    _record_usage(model_config, response, sid)
    payload = _parse_provider_json(response.content)
    if not isinstance(payload, dict):
        raise ProviderError("response_format", "模型返回的不是 JSON 对象")
    return payload, response.latency_ms


def _self_check_anchor(
    model_config: dict,
    question: dict,
    material,
    anchor: dict,
    sid: str | None = None,
) -> tuple[dict, bool]:
    """P1：审题官自检循环——让复核官批判性复核刚生成的锚点，产出修订版。

    只做一轮（控制成本与时延）；自检失败时保留原锚点，不阻断批改。
    返回 (修订后锚点, 是否发生修订)。
    """
    try:
        messages = build_zuowen_anchor_check_prompt(question, material, anchor)
        payload, _ = _model_call(model_config, messages, sid=sid)
        revised = payload.get("revised_anchor")
        if not isinstance(revised, dict):
            return anchor, False
        # 修订版补齐所有必含字段（结构必须与输入一致），防止字段丢失
        for key, default in _ANCHOR_FIELDS.items():
            if key not in revised or revised[key] in (None, "", [], {}):
                revised[key] = anchor.get(key, default)
        changed = bool(payload.get("changes"))
        return revised, changed
    except Exception as exc:
        logger.warning("审题锚点自检失败，保留原锚点：%s", exc)
        return anchor, False


def get_or_create_anchor(
    model_config: dict,
    question: dict,
    material,
    pid: str = "",
    qid: str = "",
    sid: str | None = None,
) -> tuple[dict, bool]:
    """获取审题锚点：优先缓存，未命中则调用阶段一模型生成，自检后缓存。

    锚点按模型隔离（A 模型生成的只给 A 用），因此缓存键包含 model_id。
    返回 (anchor, from_cache)。anchor 内附带 _meta（下划线开头，不进评分 prompt）。
    """
    model_id = str(model_config.get("model_id") or model_config.get("name") or "")
    anchor_key = _anchor_key(pid, qid, question, material, model_id)
    cached = _load_cached_anchor(anchor_key, model_id)
    if cached:
        return cached, True

    messages = build_zuowen_analyze_prompt(question, material)
    anchor, _ = _model_call(model_config, messages, sid=sid)

    # 锚点最小健全性校验
    if not anchor.get("core_topic") and not anchor.get("intended_theses"):
        raise ProviderError("response_format", "审题锚点缺少核心字段")

    # P1：审题官自检循环
    revised, changed = _self_check_anchor(model_config, question, material, anchor, sid=sid)
    revised = dict(revised)
    revised["_meta"] = {
        "revised": changed,
        "rounds": 1,
        "check": "审题复核官已修订锚点" if changed else "审题复核官自检通过，未修改",
    }

    try:
        _save_anchor(anchor_key, model_id, pid, qid, revised)
    except Exception:
        # 缓存失败不应中断批改
        logger.warning("保存审题锚点缓存失败", exc_info=True)
    return revised, False


def grade_essay_two_stage(
    model_config: dict,
    question: dict,
    user_answer: str,
    material,
    pid: str = "",
    qid: str = "",
    sid: str | None = None,
    deep: bool = False,
) -> dict:
    """大作文批改主入口：审题(自检) → 两阶段评分 → （可选）评审团仲裁。

    返回与旧版 grade_with_model 兼容的 dict（score_rate/dimension_scores/
    hit_points/missing_points/ai_feedback/improving_suggestions），
    额外携带两阶段特有字段（tier/anchor/paragraph_analysis/panel 等）。
    """
    # 阶段一：审题锚点（带缓存 + P1 自检）
    anchor, anchor_from_cache = get_or_create_anchor(
        model_config, question, material, pid, qid, sid=sid
    )

    # 阶段二：拿锚点评分（可选携带原始材料，严格核对「结合材料」）
    grade_with_material = bool(get_setting('essay_grade_with_material', True))
    messages = build_zuowen_grade_prompt(
        question, user_answer, anchor,
        material=material if grade_with_material else None,
    )
    payload, latency_ms = _model_call(model_config, messages, sid=sid)

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

    # P2：评审团 + 仲裁（边界分数/体裁存疑/深度批改时触发，失败自动降级）
    from src.services.grader.panel import run_review_panel

    panel = run_review_panel(
        model_config, question, user_answer, anchor, result,
        deep=deep, sid=sid,
    )
    if panel and panel.get("arbitration"):
        arb = panel["arbitration"]
        result["tier"] = arb.get("tier") or result["tier"]
        result["tier_reason"] = arb.get("tier_reason") or result["tier_reason"]
        result["score_rate"] = arb.get("score_rate") or result["score_rate"]
        if arb.get("overall_evaluation"):
            result["overall_evaluation"] = arb["overall_evaluation"]
        if arb.get("top_improvements"):
            result["top_improvements"] = arb["top_improvements"]
        result["ai_feedback"] = result["overall_evaluation"] or result["tier_reason"]
        result["improving_suggestions"] = result["top_improvements"]
    result["panel"] = panel

    return result
