"""OpenAI-compatible and Anthropic Messages provider adapters."""

import logging
import time

import requests

from src.services.grader.types import ProviderError, ProviderResponse

logger = logging.getLogger(__name__)

TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


def _endpoint(base_url: str, suffix: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ProviderError("configuration", "模型接口地址未配置")
    if base.endswith(suffix):
        return base
    if base.endswith("/v1"):
        return f"{base}/{suffix.lstrip('/')}"
    # 火山引擎等厂商使用 /v3 路径
    if base.endswith("/v3"):
        return f"{base}/{suffix.lstrip('/')}"
    return f"{base}/v1/{suffix.lstrip('/')}"


def _content_text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        texts = []
        for block in value:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                texts.append(block["text"])
        return "\n".join(texts).strip()
    return ""


class _BaseAdapter:
    def _post(self, endpoint: str, headers: dict, body: dict, config: dict) -> tuple[dict, int]:
        api_key = str(config.get("api_key") or "").strip()
        if not api_key:
            raise ProviderError("configuration", "模型 API Key 未配置")
        attempts = max(1, min(int(config.get("max_attempts", 2)), 3))
        timeout = max(5, min(int(config.get("timeout_seconds", config.get("timeout", 120))), 600))
        started = time.monotonic()
        for attempt in range(attempts):
            try:
                response = requests.post(
                    endpoint,
                    headers=headers,
                    json=body,
                    timeout=timeout,
                )
            except requests.Timeout:
                elapsed = int((time.monotonic() - started) * 1000)
                logger.warning(
                    "Model request timed out after %dms (timeout=%ss, model=%s, attempt=%d/%d)",
                    elapsed, timeout, config.get("model_name"), attempt + 1, attempts,
                )
                if attempt + 1 < attempts:
                    continue
                raise ProviderError(
                    "network", f"模型服务响应超时（>{timeout}s），请稍后重试或更换模型"
                ) from None
            except requests.RequestException as exc:
                logger.warning(
                    "Model request network error: %s: %s (model=%s, attempt=%d/%d)",
                    type(exc).__name__, exc, config.get("model_name"), attempt + 1, attempts,
                )
                if attempt + 1 < attempts:
                    continue
                raise ProviderError("network", "模型服务连接失败，请稍后重试") from None

            if response.status_code in TRANSIENT_STATUS_CODES and attempt + 1 < attempts:
                continue
            if response.status_code in (401, 403):
                raise ProviderError("authentication", "模型认证失败，请检查凭据")
            if response.status_code == 429:
                raise ProviderError("rate_limited", "模型服务限流，请稍后重试", response.status_code)
            if response.status_code >= 500:
                raise ProviderError("upstream", "模型服务暂时不可用，请稍后重试", response.status_code)
            if response.status_code >= 400:
                raise ProviderError("request", f"模型服务请求失败（HTTP {response.status_code}）", response.status_code)
            try:
                payload = response.json()
            except (ValueError, TypeError):
                raise ProviderError("response_format", "模型响应格式错误") from None
            elapsed = int((time.monotonic() - started) * 1000)
            return payload, elapsed
        raise ProviderError("upstream", "模型服务暂时不可用，请稍后重试")


class OpenAIChatAdapter(_BaseAdapter):
    def complete(self, messages: list[dict], config: dict) -> ProviderResponse:
        endpoint = _endpoint(config.get("base_url", ""), "chat/completions")
        api_key = str(config.get("api_key") or "").strip()
        payload = {
            "model": config["model_name"],
            "messages": messages,
            "temperature": config.get("temperature", 0.2),
        }
        mt = config.get("max_tokens")
        if mt and int(mt) > 0:
            payload["max_tokens"] = int(mt)
        response, latency_ms = self._post(
            endpoint,
            {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            payload,
            config,
        )
        try:
            content = _content_text(response["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            # 推理模型（如 LongCat）可能将全部 token 用于 reasoning，content 缺失
            # 尝试从 reasoning_content 提取
            try:
                reasoning = response["choices"][0]["message"].get("reasoning_content", "")
                if reasoning and isinstance(reasoning, str) and reasoning.strip():
                    # 取推理内容的最后部分作为响应（通常包含最终结论）
                    content = reasoning.strip()
                    logger.warning("Model returned only reasoning_content, using as fallback (len=%d)", len(content))
                else:
                    raise ProviderError("response_format", "模型响应格式错误（缺少 content）") from None
            except (KeyError, IndexError, TypeError):
                raise ProviderError("response_format", "模型响应格式错误（缺少 content）") from None
        if not content:
            raise ProviderError("response_format", "模型响应格式错误")
        usage = response.get("usage") or {}
        return ProviderResponse(
            content=content,
            latency_ms=latency_ms,
            raw_metadata={
                "provider": "openai",
                "model": response.get("model"),
                "request_id": response.get("id"),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            },
        )


class AnthropicMessagesAdapter(_BaseAdapter):
    def complete(self, messages: list[dict], config: dict) -> ProviderResponse:
        endpoint = _endpoint(config.get("base_url", ""), "messages")
        api_key = str(config.get("api_key") or "").strip()
        system_parts = [
            _content_text(message.get("content"))
            for message in messages
            if message.get("role") == "system"
        ]
        payload = {
            "model": config["model_name"],
            "messages": [
                {"role": message["role"], "content": message.get("content", "")}
                for message in messages
                if message.get("role") != "system"
            ],
            "temperature": config.get("temperature", 0.2),
        }
        mt = config.get("max_tokens")
        if mt and int(mt) > 0:
            payload["max_tokens"] = int(mt)
        system = "\n".join(part for part in system_parts if part)
        if system:
            payload["system"] = system
        response, latency_ms = self._post(
            endpoint,
            {
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": config.get("anthropic_version", "2023-06-01"),
            },
            payload,
            config,
        )
        content = _content_text(response.get("content"))
        if not content:
            raise ProviderError("response_format", "模型响应格式错误")
        usage = response.get("usage") or {}
        return ProviderResponse(
            content=content,
            latency_ms=latency_ms,
            raw_metadata={
                "provider": "anthropic",
                "model": response.get("model"),
                "request_id": response.get("id"),
                "prompt_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            },
        )


def adapter_for_protocol(protocol: str):
    normalized = (protocol or "").strip().lower()
    if normalized == "openai":
        return OpenAIChatAdapter()
    if normalized == "anthropic":
        return AnthropicMessagesAdapter()
    raise ProviderError("configuration", "不支持的模型协议")
