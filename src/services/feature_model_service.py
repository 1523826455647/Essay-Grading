"""功能-模型映射服务：让每个业务功能可以独立指定使用哪个 LLM 模型。

背景：此前部分功能写死模型（如素材分析写死 deepseek-v4-flash），
或统一跟随系统全局模型配置（settings 表），无法按功能精细控制。
本服务提供一张 `feature_model_mapping` 表 + 统一调用入口 `call_feature_llm`，
管理员在后台可为每个功能绑定/解绑模型；未绑定时回退到系统全局配置。
"""

import json
import logging
import re

from src.api.utils import get_db
from src.services.model_registry import get_model, list_models

logger = logging.getLogger(__name__)

# 功能注册表：key -> 展示信息（可选 token_source 指定 token 统计时的来源标签）
FEATURE_DEFINITIONS = {
    "grading": {
        "name": "答案批改（单模型）",
        "desc": "分题型评分 / 旧版评分时的单模型调用路径（多模型 ensemble 由用户自选模型）",
        "group": "批改",
    },
    "simple_feedback": {
        "name": "简化反馈",
        "desc": "免费 / 低权益用户提交后的简要评价",
        "group": "批改",
    },
    "summarize": {
        "name": "多模型结果汇总",
        "desc": "将多个模型的批改结果融合为最终答案的仲裁环节",
        "group": "批改",
    },
    "reference_extract": {
        "name": "参考答案采分点提取",
        "desc": "从参考答案中自动提取结构化采分点",
        "group": "批改",
    },
    "grading_chat": {
        "name": "批改记录对话",
        "desc": "用户就批改记录与 AI 展开多轮问答，讲解错误点与改进方向",
        "group": "批改",
        "token_source": "grading_chat",
    },
    "phrase_generate": {
        "name": "AI 造段",
        "desc": "素材学习中围绕论点生成示范段落",
        "group": "素材学习",
    },
    "topic_analyze": {
        "name": "时政热点分析",
        "desc": "对抓取的时政文章做结构化素材提炼",
        "group": "素材学习",
        "token_source": "analyze",
    },
}


def ensure_table() -> None:
    """确保 feature_model_mapping 表存在（幂等）。"""
    get_db().execute(
        """CREATE TABLE IF NOT EXISTS feature_model_mapping (
            feature_key TEXT PRIMARY KEY,
            model_id    TEXT,
            updated_at  DATETIME DEFAULT (datetime('now'))
        )"""
    )
    get_db().commit()


def get_feature_model(feature_key: str):
    """返回功能绑定的模型配置（llm_models 行，含 api_key）。

    未绑定、模型不存在或已停用时返回 None（调用方回退系统全局配置）。
    """
    if feature_key not in FEATURE_DEFINITIONS:
        return None
    ensure_table()
    row = get_db().execute(
        "SELECT model_id FROM feature_model_mapping WHERE feature_key = ?",
        (feature_key,),
    ).fetchone()
    if not row or not row["model_id"]:
        return None
    model = get_model(row["model_id"], include_secret=True)
    if not model or not model.get("enabled"):
        return None
    return model


def set_feature_model(feature_key: str, model_id: str) -> None:
    """绑定或解绑某个功能的模型。

    Args:
        feature_key: 功能键（必须在 FEATURE_DEFINITIONS 中）
        model_id: 模型 ID；传空字符串 / None 表示解绑，回退系统全局配置。
    """
    if feature_key not in FEATURE_DEFINITIONS:
        raise ValueError(f"未知功能: {feature_key}")
    model_id = (model_id or "").strip()
    ensure_table()
    db = get_db()
    if model_id:
        if not get_model(model_id):
            raise ValueError("模型不存在")
        db.execute(
            """INSERT INTO feature_model_mapping (feature_key, model_id, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(feature_key) DO UPDATE SET
                   model_id = excluded.model_id,
                   updated_at = excluded.updated_at""",
            (feature_key, model_id),
        )
    else:
        db.execute(
            "DELETE FROM feature_model_mapping WHERE feature_key = ?",
            (feature_key,),
        )
    db.commit()


def list_feature_bindings() -> list[dict]:
    """列出所有功能及其绑定模型情况（供管理员前端展示）。"""
    ensure_table()
    models = {m["model_id"]: m for m in list_models()}
    rows = get_db().execute(
        "SELECT feature_key, model_id FROM feature_model_mapping"
    ).fetchall()
    binding = {r["feature_key"]: r["model_id"] for r in rows}
    result = []
    for key, meta in FEATURE_DEFINITIONS.items():
        mid = binding.get(key)
        model = models.get(mid) if mid else None
        result.append({
            "feature_key": key,
            "name": meta["name"],
            "desc": meta["desc"],
            "group": meta["group"],
            "model_id": mid or None,
            "model_name": model["name"] if model else None,
            "model_model_name": model["model_name"] if model else None,
            "bound": bool(model),
        })
    return result


def _normalize_messages(messages):
    """把纯文本字符串包装为单条 user 消息，兼容两种调用风格。"""
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    return messages


def _parse_json_content(content: str):
    """把模型返回内容解析为 JSON 对象，容忍 markdown 代码块和前后噪声。"""
    cleaned = (content or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    raise ValueError("模型响应格式错误（非 JSON）")


def call_feature_llm(feature_key: str, messages, parse_json: bool = True):
    """功能统一 LLM 调用入口。

    优先使用该功能绑定的模型（走 provider_adapters，支持 openai/anthropic），
    未绑定则回退到系统全局模型配置（settings 表）。

    Args:
        feature_key: FEATURE_DEFINITIONS 中的功能键
        messages: 消息列表，或纯文本字符串（自动包装为 user 消息）
        parse_json: True 时把返回内容解析为 dict，否则返回原始文本

    Returns:
        dict 或 str
    """
    bound = get_feature_model(feature_key)
    if bound:
        return _call_bound_model(feature_key, bound, messages, parse_json)

    from src.config import get_llm_config
    from src.services.grader.llm_client import call_chat_completion

    return call_chat_completion(
        _normalize_messages(messages), get_llm_config(), parse_json=parse_json
    )


def _call_bound_model(feature_key: str, model: dict, messages, parse_json: bool):
    from src.services.grader.provider_adapters import adapter_for_protocol

    adapter = adapter_for_protocol(model.get("protocol"))
    response = adapter.complete(_normalize_messages(messages), model)

    # 记录 token 消耗
    try:
        meta = response.raw_metadata or {}
        source = FEATURE_DEFINITIONS.get(feature_key, {}).get("token_source", feature_key)
        from src.services import token_usage_service
        token_usage_service.record_usage(
            model_id=str(model.get("model_id") or ""),
            model_name=str(model.get("model_name") or model.get("name") or ""),
            prompt_tokens=int(meta.get("prompt_tokens") or 0),
            completion_tokens=int(meta.get("completion_tokens") or 0),
            source=source,
        )
    except Exception:
        logger.warning("记录 token 消耗失败", exc_info=True)

    if parse_json:
        return _parse_json_content(response.content)
    return response.content
