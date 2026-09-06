"""Validated client for OpenAI-compatible chat completion endpoints."""

import json
import logging
import time

import requests

logger = logging.getLogger(__name__)

TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


class LLMClientError(Exception):
    """An error safe to surface to the grading workflow."""


def build_chat_completions_endpoint(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise LLMClientError("模型接口地址未配置")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    if base.endswith("/v3"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _extract_content(payload) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMClientError("模型响应格式错误") from None
    if not isinstance(content, str) or not content.strip():
        raise LLMClientError("模型响应格式错误")
    return content.strip()


def _parse_json_content(content: str):
    cleaned = content.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    try:
        parsed = json.loads(cleaned.strip())
    except json.JSONDecodeError:
        logger.warning("Model returned invalid JSON")
        raise LLMClientError("模型响应格式错误") from None
    if not isinstance(parsed, dict):
        raise LLMClientError("模型响应格式错误")
    return parsed


def call_chat_completion(messages: list, config: dict, parse_json: bool = True):
    api_key = (config.get("api_key") or "").strip()
    if not api_key or api_key == "your-api-key-here":
        raise LLMClientError("模型 API Key 未配置")

    endpoint = build_chat_completions_endpoint(config.get("base_url", ""))
    attempts = max(1, min(int(config.get("max_attempts", 2)), 3))
    # 用户要求：模型调用最长等待 10 分钟，不做 300s 上限截断
    timeout = max(5, min(int(config.get("timeout", 600)), 600))
    retry_delay = max(0.0, min(float(config.get("retry_delay", 0.5)), 5.0))
    request_body = {
        "model": config["model"],
        "messages": messages,
        "temperature": config.get("temperature", 0.3),
    }
    # max_tokens 不再限制：无论配置什么值都不向模型 API 传 max_tokens，
    # 让模型按其自身上限输出，避免评分/建议被截断

    for attempt in range(attempts):
        try:
            response = requests.post(
                endpoint,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                json=request_body,
                timeout=timeout,
            )
        except requests.RequestException:
            if attempt + 1 < attempts:
                if retry_delay:
                    time.sleep(retry_delay)
                continue
            logger.warning("Model request failed after %s attempts", attempts)
            raise LLMClientError("模型服务连接失败，请稍后重试") from None

        if response.status_code in TRANSIENT_STATUS_CODES and attempt + 1 < attempts:
            if retry_delay:
                time.sleep(retry_delay)
            continue
        if response.status_code >= 400:
            logger.warning("Model request returned HTTP %s", response.status_code)
            raise LLMClientError(f"模型服务请求失败（HTTP {response.status_code}）")

        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise LLMClientError("模型响应格式错误") from None
        content = _extract_content(payload)
        return _parse_json_content(content) if parse_json else content

    raise LLMClientError("模型服务暂时不可用，请稍后重试")
