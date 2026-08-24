"""Concurrent model execution for fallback and ensemble modes."""

from concurrent.futures import ThreadPoolExecutor
import os

from src.services.grader.types import JudgeResult, ProviderError


def _configured_parallel_limit() -> int:
    try:
        value = int(os.getenv("MAX_PARALLEL_MODELS", "4"))
    except (TypeError, ValueError):
        value = 4
    return max(1, min(value, 4))


def _failed_result(model_id: str, error_code: str) -> JudgeResult:
    return JudgeResult(model_id=model_id, status="failed", error_code=error_code)


def _call(judge_fn, config: dict) -> JudgeResult:
    model_id = config["model_id"]
    try:
        result = judge_fn(config)
        if not isinstance(result, JudgeResult):
            raise TypeError("judge_fn must return JudgeResult")
        result.model_id = model_id
        if result.status == "completed" and result.score_rate is None:
            result.status = "failed"
            result.error_code = "response_format"
        return result
    except ProviderError as error:
        return _failed_result(model_id, error.code)
    except Exception:
        return _failed_result(model_id, "internal")


def run_fallback(model_configs: list[dict], judge_fn, deadline: float | None = None) -> list[JudgeResult]:
    from src.services.grader.deadline import expired
    judgments = []
    for config in sorted(model_configs, key=lambda item: int(item.get("priority", 100))):
        if expired(deadline):
            # 预算不足，不再尝试下一个模型，尽快返回已得结果
            break
        judgment = _call(judge_fn, config)
        judgments.append(judgment)
        if judgment.status == "completed" and judgment.score_rate is not None:
            break
    return judgments


def run_ensemble(
    model_configs: list[dict],
    judge_fn,
    max_parallel_models: int | None = None,
) -> list[JudgeResult]:
    configs = list(model_configs)
    if not configs:
        return []
    parallel_limit = (
        _configured_parallel_limit()
        if max_parallel_models is None
        else max(1, min(int(max_parallel_models), 4))
    )
    worker_count = max(1, min(parallel_limit, len(configs)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(_call, judge_fn, config) for config in configs]
        return [future.result() for future in futures]
