"""批改截止时间（deadline）机制。

根因背景：gunicorn worker 超时（300s）会直接 SIGKILL 请求，导致批改
既无结果也无 pending_review 标记（用户看到"没有任何报告"）。模型调用
超时 + JSON 重试叠加后，最坏耗时可能达到 2×模型超时（如 2×180s=360s），
超过 worker 超时被杀。

本模块提供：
- make_deadline：请求级截止时间（默认 250s，给 gunicorn 300s 留持久化余量）
- expired：是否已到/临近截止
- budget_timeout：单次模型调用的有效超时 = min(配置超时, 剩余预算)，
  保证任何单次调用都不会把请求拖过 worker 超时
"""
import time

# gunicorn --timeout 300；留 50s 给持久化/聚合/响应
DEFAULT_GRADING_DEADLINE_SECONDS = 250
# 单次调用至少给多少秒（避免预算过小导致必然失败）
MIN_CALL_TIMEOUT_SECONDS = 8
# 距截止多少秒内视为"已到期"（不再发起新调用）
EXPIRY_BUFFER_SECONDS = 5


def make_deadline(seconds: float = DEFAULT_GRADING_DEADLINE_SECONDS) -> float:
    """创建请求级截止时间（monotonic 时钟）。"""
    return time.monotonic() + seconds


def remaining(deadline: float | None) -> float | None:
    """剩余秒数；无 deadline 返回 None。"""
    if deadline is None:
        return None
    return deadline - time.monotonic()


def expired(deadline: float | None, buffer: float = EXPIRY_BUFFER_SECONDS) -> bool:
    """是否已到/临近截止（buffer 秒内视为到期，不再发起新调用）。"""
    if deadline is None:
        return False
    return time.monotonic() > deadline - buffer


def budget_timeout(configured_timeout: int | float | None, deadline: float | None,
                   minimum: int = MIN_CALL_TIMEOUT_SECONDS,
                   reserve: int = EXPIRY_BUFFER_SECONDS) -> int:
    """单次模型调用的有效超时：不超过截止时间的剩余预算。"""
    if deadline is None:
        return int(configured_timeout or 120)
    budget = int(remaining(deadline)) - reserve
    if budget < minimum:
        return minimum
    try:
        configured = int(configured_timeout or 120)
    except (TypeError, ValueError):
        configured = 120
    return max(minimum, min(configured, budget))
