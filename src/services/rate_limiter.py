"""内存级请求限流与失败锁定（每 worker 独立计数）。

用于登录暴力破解防护与免登录接口限流。多 worker 部署下各 worker 独立
计数，阈值为近似值（实际上限 ≈ 阈值 × worker 数），但对爆破/滥用面的
削减依然有效；服务器未部署 Redis，故不依赖外部存储。
"""
import threading
import time

_lock = threading.Lock()
# (scope, key) -> [count, window_start]
_windows = {}
# (scope, key) -> locked_until(monotonic)
_locks = {}


def _now() -> float:
    return time.monotonic()


def is_locked(scope: str, key: str) -> int:
    """返回剩余锁定秒数，0 表示未锁定。"""
    with _lock:
        until = _locks.get((scope, key))
        if until and until > _now():
            return int(until - _now()) + 1
        if until:
            _locks.pop((scope, key), None)
        return 0


def register_failure(scope: str, key: str, max_fails: int = 5, lock_seconds: int = 900) -> int:
    """记录一次失败，达到阈值后锁定。返回触发的锁定秒数（0=未触发）。"""
    now = _now()
    with _lock:
        count, start = _windows.get((scope, key), (0, now))
        if now - start > lock_seconds:
            count, start = 0, now
        count += 1
        _windows[(scope, key)] = (count, start)
        if count >= max_fails:
            _locks[(scope, key)] = now + lock_seconds
            _windows.pop((scope, key), None)
            return lock_seconds
        return 0


def reset(scope: str, key: str) -> None:
    """成功后清除失败计数。"""
    with _lock:
        _windows.pop((scope, key), None)


def rate_allow(scope: str, key: str, limit: int, window_seconds: int = 3600) -> bool:
    """固定窗口限流：窗口内超过 limit 次则拒绝（返回 False）。"""
    now = _now()
    with _lock:
        count, start = _windows.get((scope, key), (0, now))
        if now - start > window_seconds:
            count, start = 0, now
        if count >= limit:
            _windows[(scope, key)] = (count, start)
            return False
        _windows[(scope, key)] = (count + 1, start)
        return True


def clear_expired(max_age_seconds: int = 7200) -> None:
    """清理过期条目，防止长期运行的内存增长。"""
    now = _now()
    with _lock:
        for k in [k for k, (_c, s) in list(_windows.items()) if now - s > max_age_seconds]:
            _windows.pop(k, None)
        for k in [k for k, u in list(_locks.items()) if u <= now]:
            _locks.pop(k, None)
