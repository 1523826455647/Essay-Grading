"""
套餐服务 - 套餐定义、用户套餐、套餐兑换、扣减（含按天套餐每日限额）
"""

from datetime import datetime, timedelta, timezone
from src.api.utils import get_db


# 中国时区（用户所在时区，按天限额以北京日期为准）
_CN_TZ = timezone(timedelta(hours=8))


def _today_cn() -> str:
    """获取中国时区今天日期（YYYY-MM-DD），每日限额重置依据"""
    return datetime.now(_CN_TZ).strftime('%Y-%m-%d')


def _daily_remaining(pkg: dict) -> int | None:
    """计算按天套餐今日剩余次数；None 表示不限次"""
    limit = pkg.get("daily_limit") or 0
    if limit <= 0:
        return None  # 不限
    used = pkg.get("daily_used") or 0
    if pkg.get("daily_date") != _today_cn():
        used = 0  # 跨天自动重置
    return max(0, limit - used)


# ── 套餐 CRUD ──────────────────────────────────────────────

def list_packages(active_only: bool = False) -> list[dict]:
    """获取所有套餐列表（含兑换统计）"""
    db = get_db()
    query = """
        SELECT p.*, (
            SELECT COUNT(*) FROM user_packages up WHERE up.package_id = p.id
        ) AS redemption_count
        FROM packages p
    """
    if active_only:
        query += " WHERE p.is_active = 1"
    query += " ORDER BY p.sort_order ASC, p.id ASC"
    rows = db.execute(query).fetchall()
    return [dict(r) for r in rows]


def get_package(package_id: int) -> dict | None:
    """获取单个套餐"""
    db = get_db()
    row = db.execute("SELECT * FROM packages WHERE id = ?", (package_id,)).fetchone()
    return dict(row) if row else None


def create_package(data: dict) -> int:
    """创建套餐，返回套餐 ID"""
    db = get_db()
    now = datetime.now().isoformat()
    cur = db.execute(
        """INSERT INTO packages (name, description, package_type, credits, duration_days,
           daily_limit, price, badge_name, badge_color, sort_order, is_active, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data.get('name', ''),
            data.get('description', ''),
            data.get('package_type', 'usage'),
            int(data.get('credits', 0)),
            int(data.get('duration_days', 0)),
            int(data.get('daily_limit', 0)),
            float(data.get('price', 0)),
            data.get('badge_name', ''),
            data.get('badge_color', '#0c8ee7'),
            int(data.get('sort_order', 0)),
            int(data.get('is_active', 1)),
            now,
            now,
        ),
    )
    db.commit()
    return cur.lastrowid


def update_package(package_id: int, data: dict) -> bool:
    """更新套餐"""
    db = get_db()
    now = datetime.now().isoformat()
    db.execute(
        """UPDATE packages SET name=?, description=?, package_type=?, credits=?,
           duration_days=?, daily_limit=?, price=?, badge_name=?, badge_color=?,
           sort_order=?, is_active=?, updated_at=?
           WHERE id=?""",
        (
            data.get('name', ''),
            data.get('description', ''),
            data.get('package_type', 'usage'),
            int(data.get('credits', 0)),
            int(data.get('duration_days', 0)),
            int(data.get('daily_limit', 0)),
            float(data.get('price', 0)),
            data.get('badge_name', ''),
            data.get('badge_color', '#0c8ee7'),
            int(data.get('sort_order', 0)),
            int(data.get('is_active', 1)),
            now,
            package_id,
        ),
    )
    db.commit()
    return True


def delete_package(package_id: int) -> bool:
    """软删除套餐（设为禁用）"""
    db = get_db()
    db.execute(
        "UPDATE packages SET is_active = 0, updated_at = ? WHERE id = ?",
        (datetime.now().isoformat(), package_id),
    )
    db.commit()
    return True


# ── 用户套餐 ──────────────────────────────────────────────

def _get_time_packages(db, uid: str) -> list[dict]:
    """获取用户所有生效的按天套餐（按到期时间倒序）"""
    now = datetime.now().isoformat()
    rows = db.execute(
        """SELECT * FROM user_packages
           WHERE uid = ? AND is_active = 1 AND package_type = 'time'
             AND (expires_at IS NULL OR expires_at > ?)
           ORDER BY expires_at DESC""",
        (uid, now),
    ).fetchall()
    return [dict(r) for r in rows]


def get_user_active_package(uid: str) -> dict | None:
    """
    获取用户当前生效的套餐（与扣减优先级一致）：
    1. 今日还有额度的按天套餐（不限次优先展示）
    2. 有剩余次数的按次套餐
    3. 兜底：任一按天套餐（今日额度耗尽时的展示）
    """
    db = get_db()

    time_pkgs = _get_time_packages(db, uid)
    usable = [p for p in time_pkgs if _daily_remaining(p) != 0]
    if usable:
        return usable[0]

    usage = db.execute(
        """SELECT * FROM user_packages
           WHERE uid = ? AND is_active = 1 AND package_type = 'usage'
             AND remaining_credits > 0
           ORDER BY id DESC LIMIT 1""",
        (uid,),
    ).fetchone()
    if usage:
        return dict(usage)

    if time_pkgs:
        return time_pkgs[0]

    return None


def get_user_package_balance(uid: str) -> dict:
    """获取用户套餐余额信息（含每日限额使用情况）"""
    pkg = get_user_active_package(uid)
    if not pkg:
        return {"has_package": False}

    result = {
        "has_package": True,
        "package_name": pkg["package_name"],
        "package_type": pkg["package_type"],
        "badge_name": pkg["badge_name"],
        "badge_color": pkg["badge_color"],
    }

    if pkg["package_type"] == "time":
        if pkg["expires_at"]:
            try:
                expire_dt = datetime.fromisoformat(pkg["expires_at"])
                remaining = (expire_dt - datetime.now()).days
                result["remaining_days"] = max(0, remaining)
                result["expires_at"] = pkg["expires_at"]
            except (ValueError, TypeError):
                result["remaining_days"] = 0
                result["expires_at"] = None
        else:
            result["remaining_days"] = -1  # 永久
            result["expires_at"] = None

        # 每日限额信息
        result["daily_limit"] = pkg.get("daily_limit") or 0
        remaining_today = _daily_remaining(pkg)
        if remaining_today is None:
            result["remaining_today"] = -1  # 今日不限次
        else:
            result["remaining_today"] = remaining_today
            result["daily_used"] = (pkg.get("daily_used") or 0) if pkg.get("daily_date") == _today_cn() else 0
    else:
        result["remaining_credits"] = pkg["remaining_credits"]
        result["total_credits"] = pkg["total_credits"]

    return result


def get_user_packages(uid: str) -> list[dict]:
    """获取用户所有套餐记录"""
    db = get_db()
    rows = db.execute(
        """SELECT * FROM user_packages WHERE uid = ?
           ORDER BY created_at DESC""",
        (uid,),
    ).fetchall()
    return [dict(r) for r in rows]


def redeem_package(uid: str, package: dict, code: str) -> dict:
    """为用户兑换套餐（创建 user_package 记录）"""
    db = get_db()
    now = datetime.now().isoformat()

    package_type = package["package_type"]
    credits = int(package.get("credits", 0))
    duration_days = int(package.get("duration_days", 0))
    daily_limit = int(package.get("daily_limit", 0))

    expires_at = None
    if package_type == "time" and duration_days > 0:
        expires_at = (datetime.now() + timedelta(days=duration_days)).isoformat()

    db.execute(
        """INSERT INTO user_packages
           (uid, package_id, code, package_name, package_type, total_credits,
            remaining_credits, total_days, daily_limit, daily_used, daily_date,
            badge_name, badge_color, is_active, expires_at, redeemed_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 1, ?, ?, ?)""",
        (
            uid,
            package["id"],
            code,
            package["name"],
            package_type,
            credits,
            credits,  # remaining = total
            duration_days,
            daily_limit,
            _today_cn(),  # 兑换当天开始计数
            package.get("badge_name", ""),
            package.get("badge_color", "#0c8ee7"),
            expires_at,
            now,
            now,
        ),
    )
    db.commit()

    return {
        "package_name": package["name"],
        "package_type": package_type,
        "credits": credits,
        "duration_days": duration_days,
        "daily_limit": daily_limit,
        "expires_at": expires_at,
        "badge_name": package.get("badge_name", ""),
    }


def has_available_grading(uid: str) -> bool:
    """
    用户今日是否还有可用的套餐批改额度：
    - 任一按天套餐：未过期且（不限次 或 今日未用完）
    - 任一按次套餐：剩余次数 > 0
    """
    db = get_db()

    time_pkgs = _get_time_packages(db, uid)
    if any(_daily_remaining(p) != 0 for p in time_pkgs):
        return True

    usage_pkg = db.execute(
        """SELECT 1 FROM user_packages
           WHERE uid = ? AND is_active = 1 AND package_type = 'usage'
             AND remaining_credits > 0 LIMIT 1""",
        (uid,),
    ).fetchone()
    return bool(usage_pkg)


def deduct_package_credit(uid: str) -> tuple[bool, str]:
    """
    从用户活跃套餐扣减 1 次。返回 (是否扣减成功, 描述)。
    - 按天不限次：直接通过
    - 按天限额：遍历所有生效按天套餐，用今日还有额度的那个计数扣减
    - 均不可用后回退到按次套餐
    - 按次套餐：扣减 remaining_credits，用完自动标记 inactive
    """
    db = get_db()
    today = _today_cn()

    # 1. 遍历按天套餐，找今日还有额度的
    for time_pkg in _get_time_packages(db, uid):
        remaining_today = _daily_remaining(time_pkg)
        if remaining_today is None:
            # 不限次
            return True, f"套餐「{time_pkg['package_name']}」有效期内不限次"
        if remaining_today > 0:
            # 计数扣减（跨天自动重置）
            used = time_pkg["daily_used"] or 0
            if time_pkg["daily_date"] != today:
                used = 0
            new_used = used + 1
            db.execute(
                "UPDATE user_packages SET daily_used = ?, daily_date = ? WHERE id = ?",
                (new_used, today, time_pkg["id"]),
            )
            db.commit()
            return True, f"套餐「{time_pkg['package_name']}」今日剩余 {remaining_today - 1} 次"

    # 2. 按天套餐均不可用，回退到按次套餐
    usage_pkg = db.execute(
        """SELECT * FROM user_packages
           WHERE uid = ? AND is_active = 1 AND package_type = 'usage'
             AND remaining_credits > 0
           ORDER BY id ASC LIMIT 1""",
        (uid,),
    ).fetchone()

    if usage_pkg:
        new_remaining = usage_pkg["remaining_credits"] - 1
        if new_remaining <= 0:
            db.execute(
                "UPDATE user_packages SET remaining_credits = 0, is_active = 0 WHERE id = ?",
                (usage_pkg["id"],),
            )
        else:
            db.execute(
                "UPDATE user_packages SET remaining_credits = ? WHERE id = ?",
                (new_remaining, usage_pkg["id"]),
            )
        db.commit()
        return True, f"套餐「{usage_pkg['package_name']}」剩余 {new_remaining} 次"

    return False, "无可用套餐额度"


def expire_stale_packages() -> int:
    """将已过期的按天套餐标记为 inactive，返回处理数量"""
    db = get_db()
    now = datetime.now().isoformat()
    cur = db.execute(
        """UPDATE user_packages SET is_active = 0
           WHERE package_type = 'time' AND is_active = 1
             AND expires_at IS NOT NULL AND expires_at <= ?""",
        (now,),
    )
    db.commit()
    return cur.rowcount