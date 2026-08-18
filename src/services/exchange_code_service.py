"""
兑换码服务 - 生成、校验、核销兑换码
"""

import uuid
import hashlib
import secrets
import sqlite3
from datetime import datetime
from src.api.utils import get_db


def _log_transaction(uid: str, trans_type: str, amount: float, balance: float, detail: str = "", commit: bool = False):
    """记录积分变动日志"""
    db = get_db()
    db.execute(
        """INSERT INTO credit_transactions (uid, trans_type, amount, balance, detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (uid, trans_type, amount, balance, detail, datetime.now().isoformat())
    )
    if commit:
        db.commit()


MODEL_CREDIT_COST = {
    "deepseek": 1,
    "gpt": 1,
    "claude": 2,
    "stepfun": 0.5,
}


def _default_credit_cost(name: str, model_name: str) -> float:
    """根据模型名称推断默认消耗次数"""
    key = (name + " " + model_name).lower()
    if "deepseek" in key:
        return 1.0
    if "claude" in key:
        return 2.0
    if "step" in key:
        return 0.5
    return 1.0


def calculate_credit_cost(model_ids: list[str]) -> float:
    """计算所选模型的总消耗次数"""
    db = get_db()
    total = 0.0
    for mid in model_ids:
        row = db.execute(
            "SELECT credit_cost, name, model_name FROM llm_models WHERE model_id=? AND deleted_at IS NULL",
            (mid,)
        ).fetchone()
        if row and row["credit_cost"] is not None:
            total += float(row["credit_cost"])
        elif row:
            total += _default_credit_cost(row["name"], row["model_name"])
        else:
            total += 1.0
    return round(total, 1)


def get_user_credits(uid: str) -> float:
    """获取用户剩余批改次数（管理员返回无穷大）"""
    db = get_db()
    user = db.execute(
        "SELECT role, grading_credits FROM users WHERE uid=?", (uid,)
    ).fetchone()
    if not user:
        return 0.0
    if user["role"] in ("admin", "super_admin"):
        return float("inf")
    return float(user["grading_credits"] or 0)


def deduct_credits(uid: str, amount: float) -> float:
    """扣减用户批改次数，返回剩余次数"""
    db = get_db()
    user = db.execute(
        "SELECT role, grading_credits FROM users WHERE uid=?", (uid,)
    ).fetchone()
    if not user or user["role"] in ("admin", "super_admin"):
        return float("inf")
    current = float(user["grading_credits"] or 0)
    new_balance = max(0.0, round(current - amount, 1))
    db.execute(
        "UPDATE users SET grading_credits=? WHERE uid=?", (new_balance, uid)
    )
    _log_transaction(uid, "consume", -amount, new_balance, f"批改消耗 {amount} 次")
    db.commit()
    return new_balance


def generate_code(credits: float, max_uses: int = 1, prefix: str = "SLB",
                  created_by: str = "", expires_days: int = 365) -> str:
    """生成一个兑换码"""
    db = get_db()
    random_part = secrets.token_hex(4).upper()
    code = f"{prefix}-{random_part[:4]}-{random_part[4:]}"

    expires_at = datetime.now().isoformat() if expires_days <= 0 else None
    if expires_days > 0:
        from datetime import timedelta
        expires_at = (datetime.now() + timedelta(days=expires_days)).isoformat()

    db.execute(
        """INSERT INTO exchange_codes
           (code, credits, max_uses, used_count, created_by, expires_at, status)
           VALUES (?, ?, ?, 0, ?, ?, 'active')""",
        (code, credits, max_uses, created_by, expires_at)
    )
    db.commit()
    return code


def batch_generate(credits: float, count: int, max_uses: int = 1,
                   prefix: str = "SLB", created_by: str = "",
                   expires_days: int = 365) -> list[str]:
    """批量生成兑换码"""
    codes = []
    for _ in range(count):
        code = generate_code(credits, max_uses, prefix, created_by, expires_days)
        codes.append(code)
    return codes


def redeem_code(uid: str, code: str) -> dict:
    """用户兑换一个兑换码"""
    db = get_db()
    code_row = db.execute(
        """SELECT * FROM exchange_codes WHERE code=? AND status='active'""",
        (code,)
    ).fetchone()

    if not code_row:
        return {"success": False, "message": "兑换码无效或已过期"}

    if code_row["max_uses"] > 0 and code_row["used_count"] >= code_row["max_uses"]:
        return {"success": False, "message": "该兑换码已被使用完"}

    if code_row["expires_at"]:
        if datetime.now().isoformat() > code_row["expires_at"]:
            db.execute(
                "UPDATE exchange_codes SET status='expired' WHERE code=?",
                (code,)
            )
            db.commit()
            return {"success": False, "message": "兑换码已过期"}

    credits = float(code_row["credits"])

    # 记录兑换
    db.execute(
        """INSERT INTO code_redemptions (code, uid, credits_granted, redeemed_at)
           VALUES (?, ?, ?, ?)""",
        (code, uid, credits, datetime.now().isoformat())
    )

    # 增加用户次数
    db.execute(
        "UPDATE users SET grading_credits = COALESCE(grading_credits, 0) + ? WHERE uid = ?",
        (credits, uid)
    )

    # 更新兑换码使用次数
    new_used = code_row["used_count"] + 1
    new_status = "used" if code_row["max_uses"] > 0 and new_used >= code_row["max_uses"] else "active"
    db.execute(
        "UPDATE exchange_codes SET used_count=?, status=? WHERE code=?",
        (new_used, new_status, code)
    )

    # 获取最新余额
    user_after = db.execute(
        "SELECT grading_credits FROM users WHERE uid=?", (uid,)
    ).fetchone()
    balance = float(user_after["grading_credits"] or 0) if user_after else credits

    # 记录交易日志
    _log_transaction(uid, "recharge", credits, balance, f"兑换码 {code} 充值 {credits} 次")

    db.commit()

    return {
        "success": True,
        "message": f"兑换成功！获得 {credits} 次批改机会",
        "credits_granted": credits,
        "balance": balance,
    }


def list_codes(page: int = 1, per_page: int = 20):
    """管理员查询兑换码列表"""
    db = get_db()
    offset = (page - 1) * per_page
    total = db.execute(
        "SELECT COUNT(*) as cnt FROM exchange_codes"
    ).fetchone()["cnt"]

    rows = db.execute(
        """SELECT * FROM exchange_codes
           ORDER BY created_at DESC LIMIT ? OFFSET ?""",
        (per_page, offset)
    ).fetchall()

    return {
        "codes": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "pages": max(1, (total + per_page - 1) // per_page),
    }


def disable_code(code: str) -> bool:
    """管理员禁用兑换码"""
    db = get_db()
    db.execute(
        "UPDATE exchange_codes SET status='disabled' WHERE code=?",
        (code,)
    )
    db.commit()
    return True
