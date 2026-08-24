from flask import Blueprint, request
import os, uuid, jwt, requests
from datetime import datetime, timedelta
from datetime import date, datetime, timedelta

from src.services.auth import register_user, login_user, logout_user, get_user_profile, is_vip_user
from src.services.url_safety import safe_get
from src.api.utils import api_success, api_error, token_required, get_db

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')


@auth_bp.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    if not data:
        return api_error("请提供用户名和密码", 400)

    username = data.get('username', '').strip()
    password = data.get('password', '')
    nickname = data.get('nickname', '').strip()

    if not username or not password:
        return api_error("用户名和密码不能为空", 400)

    if len(username) < 3 or len(username) > 30:
        return api_error("用户名长度需在3-30个字符之间", 400)

    if len(password) < 6 or len(password) > 100:
        return api_error("密码长度需在6-100个字符之间", 400)

    if nickname and len(nickname) > 50:
        return api_error("昵称不能超过50个字符", 400)

    result, err = register_user(username, password, nickname)
    if err:
        return api_error(err, 400)

    return api_success(result)


@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    if not data:
        return api_error("请提供用户名和密码", 400)

    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not username or not password:
        return api_error("用户名和密码不能为空", 400)

    # 暴力破解防护：IP 与用户名任一锁定即拒绝
    from src.services import rate_limiter
    ip = request.remote_addr or 'unknown'
    for scope_key in ((ip, 'ip'), (username.lower(), 'user')):
        remaining = rate_limiter.is_locked('login', scope_key[0])
        if remaining:
            return api_error(f"尝试次数过多，请 {remaining // 60 + 1} 分钟后再试", 429)

    result, err = login_user(username, password)
    if err:
        # 失败计数：IP 维度与用户名维度
        rate_limiter.register_failure('login', ip, max_fails=10, lock_seconds=900)
        rate_limiter.register_failure('login', username.lower(), max_fails=5, lock_seconds=900)
        return api_error(err, 401)

    rate_limiter.reset('login', ip)
    rate_limiter.reset('login', username.lower())
    return api_success(result)


@auth_bp.route('/logout', methods=['POST'])
@token_required
def logout(current_user):
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    logout_user(current_user['uid'], token=token)
    return api_success(message="已退出登录")


@auth_bp.route('/me', methods=['GET'])
@token_required
def me(current_user):
    profile = get_user_profile(current_user['uid'])
    if not profile:
        return api_error("用户不存在", 404)

    profile['is_vip'] = is_vip_user(profile)
    return api_success(profile)


@auth_bp.route('/me', methods=['PUT'])
@token_required
def update_profile(current_user):
    """更新个人资料（昵称、邮箱、手机、个人简介）"""
    data = request.get_json(silent=True) or {}
    uid = current_user['uid']
    db = get_db()

    allowed = ('nickname', 'email', 'phone')
    updates = {}
    for field in allowed:
        if field in data:
            val = str(data[field]).strip() if data[field] else ''
            if field == 'nickname' and len(val) > 50:
                return api_error("昵称不能超过50个字符", 400)
            if field == 'email' and val and '@' not in val:
                return api_error("邮箱格式不正确", 400)
            if field == 'phone' and val and len(val) > 20:
                return api_error("手机号过长", 400)
            updates[field] = val

    # bio 存储在 settings JSON 中
    if 'bio' in data:
        bio = str(data['bio']).strip() if data['bio'] else ''
        if len(bio) > 500:
            return api_error("个人简介不能超过500字", 400)
        import json
        user = db.execute("SELECT settings FROM users WHERE uid = ?", (uid,)).fetchone()
        try:
            settings = json.loads(user['settings']) if user and user['settings'] else {}
        except (json.JSONDecodeError, TypeError):
            settings = {}
        settings['bio'] = bio
        updates['settings'] = json.dumps(settings, ensure_ascii=False)

    if not updates:
        return api_error("没有需要更新的字段", 400)

    set_clause = ', '.join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [uid]
    db.execute(f"UPDATE users SET {set_clause} WHERE uid = ?", values)
    db.commit()

    return api_success({"updated": list(updates.keys()), "message": "资料已更新"})


@auth_bp.route('/password', methods=['PUT'])
@token_required
def change_password(current_user):
    """修改密码"""
    data = request.get_json(silent=True) or {}
    old = (data.get('old_password') or '').strip()
    new = (data.get('new_password') or '').strip()
    confirm = (data.get('confirm_password') or '').strip()

    if not old or not new:
        return api_error("请填写当前密码和新密码", 400)
    if len(new) < 6 or len(new) > 100:
        return api_error("新密码长度需在6-100个字符之间", 400)
    if new != confirm:
        return api_error("两次输入的新密码不一致", 400)

    db = get_db()
    user = db.execute("SELECT password_hash FROM users WHERE uid = ?", (current_user['uid'],)).fetchone()
    if not user:
        return api_error("用户不存在", 404)

    import bcrypt
    try:
        if not bcrypt.checkpw(old.encode('utf-8'), user['password_hash'].encode('utf-8')):
            return api_error("当前密码错误", 400)
    except Exception:
        return api_error("密码验证失败", 500)

    new_hash = bcrypt.hashpw(new.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    db.execute("UPDATE users SET password_hash = ? WHERE uid = ?", (new_hash, current_user['uid']))
    db.commit()

    return api_success({"message": "密码已修改"})




@auth_bp.route('/wx-login', methods=['POST'])
def wx_login():
    data = request.get_json(silent=True) or {}
    code = (data.get('code') or '').strip()
    if not code:
        return api_error('Missing code', 400)

    appid = (os.getenv('WX_APPID') or '').strip()
    secret = (os.getenv('WX_SECRET') or '').strip()
    if not appid or not secret:
        return api_error('WX not configured', 500)

    try:
        url = f'https://api.weixin.qq.com/sns/jscode2session?appid={appid}&secret={secret}&js_code={code}&grant_type=authorization_code'
        r = safe_get(url, timeout=10)
        wx = r.json()
    except Exception as e:
        return api_error(f'WeChat error: {e}', 502)

    openid = wx.get('openid')
    if not openid:
        return api_error(f'WX: {wx.get("errmsg", "unknown")}', 401)

    nickname = (data.get('nickname') or '').strip() or u'微信用户'
    avatar = (data.get('avatar') or '').strip()
    unionid = wx.get('unionid', '')

    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE wx_openid = ? AND status = 'active'",
        (openid,)
    ).fetchone()

    if user:
        uid = user['uid']
        db.execute(
            "UPDATE users SET nickname=?, avatar_url=?, last_login=datetime('now') WHERE uid=?",
            (nickname, avatar, uid)
        )
        db.commit()
    else:
        uid = 'wx_' + uuid.uuid4().hex[:16]
        username = 'wx_' + openid[:12]
        db.execute(
            "INSERT INTO users (uid, username, password_hash, nickname, avatar_url,"
            " wx_openid, wx_unionid, role, status, grading_credits)"
            " VALUES (?, ?, '', ?, ?, ?, ?, 'user', 'active', 1.0)",
            (uid, username, nickname, avatar, openid, unionid)
        )
        db.execute(
            "INSERT INTO credit_transactions (uid, trans_type, amount, balance, detail, created_at)"
            " VALUES (?, 'grant', 1.0, 1.0, '微信新用户注册赠送', datetime('now'))",
            (uid,)
        )
        db.commit()

    SECRET_KEY = os.getenv('JWT_SECRET', 'shenlun-bang-jwt-secret-key')
    token = jwt.encode({
        'sub': uid,
        'iat': datetime.utcnow(),
        'exp': datetime.utcnow() + timedelta(days=30),
        'jti': uuid.uuid4().hex[:16],
    }, SECRET_KEY, algorithm='HS256')

    from src.services.auth import get_user_profile
    profile = get_user_profile(uid) or {}
    profile['token'] = token
    return api_success(profile)

# ============================================================
# 每日签到系统
# ============================================================

SIGN_IN_REWARDS = {
    1: 5, 2: 5, 3: 10, 4: 10, 5: 15, 6: 15, 7: 30  # 连续7天大奖
}


@auth_bp.route('/signin', methods=['POST'])
@token_required
def sign_in(current_user):
    """每日签到"""
    uid = current_user['uid']
    today = date.today().isoformat()
    db = get_db()

    # Check if already signed in today
    existing = db.execute(
        "SELECT id FROM sign_in_records WHERE uid = ? AND sign_date = ?",
        (uid, today)
    ).fetchone()
    if existing:
        return api_error("今日已签到，请明天再来", 400)

    # Calculate streak
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    last_sign = db.execute(
        "SELECT sign_date, streak_days FROM sign_in_records WHERE uid = ? ORDER BY sign_date DESC LIMIT 1",
        (uid,)
    ).fetchone()

    streak = 1
    if last_sign:
        last_date = last_sign['sign_date']
        if last_date == yesterday:
            streak = min(last_sign['streak_days'] + 1, 7)
        elif last_date == today:
            return api_error("今日已签到", 400)

    # Calculate reward
    reward = SIGN_IN_REWARDS.get(streak, 5)

    # Insert sign-in record
    db.execute(
        "INSERT INTO sign_in_records (uid, sign_date, streak_days, reward_points) VALUES (?, ?, ?, ?)",
        (uid, today, streak, reward)
    )

    # Update user points
    db.execute(
        """INSERT INTO user_points (uid, total_points, updated_at) VALUES (?, ?, datetime('now'))
           ON CONFLICT(uid) DO UPDATE SET total_points = total_points + ?, updated_at = datetime('now')""",
        (uid, reward, reward)
    )

    # Record learning activity
    db.execute(
        "INSERT INTO learning_records (uid, action, target_id, created_at) VALUES (?, 'sign_in', ?, datetime('now'))",
        (uid, today)
    )

    db.commit()

    return api_success({
        'streak_days': streak,
        'reward_points': reward,
        'message': f'签到成功！连续签到{streak}天，获得{reward}积分'
    })


@auth_bp.route('/signin/status', methods=['GET'])
@token_required
def sign_in_status(current_user):
    """获取签到状态"""
    uid = current_user['uid']
    today = date.today().isoformat()
    db = get_db()

    # Today's sign-in
    today_sign = db.execute(
        "SELECT id FROM sign_in_records WHERE uid = ? AND sign_date = ?",
        (uid, today)
    ).fetchone()

    # Current streak
    last_sign = db.execute(
        "SELECT streak_days FROM sign_in_records WHERE uid = ? ORDER BY sign_date DESC LIMIT 1",
        (uid,)
    ).fetchone()

    # Total points
    points = db.execute(
        "SELECT total_points, used_points FROM user_points WHERE uid = ?",
        (uid,)
    ).fetchone()

    # This month's sign-in count
    month_count = db.execute(
        "SELECT COUNT(*) FROM sign_in_records WHERE uid = ? AND sign_date >= date('now', 'start of month')",
        (uid,)
    ).fetchone()[0]

    return api_success({
        'signed_today': today_sign is not None,
        'current_streak': last_sign['streak_days'] if last_sign else 0,
        'total_points': points['total_points'] if points else 0,
        'used_points': points['used_points'] if points else 0,
        'month_sign_ins': month_count
    })
