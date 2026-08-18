import json
import time

from flask import Blueprint, request, session

from src.api.utils import (
    api_success,
    api_error,
    admin_required,
    get_db,
    clamp_per_page,
    _extract_user_from_token,
)
from src.services import paper_service, phrase_service
from src.services.auth import get_user_profile, is_vip_user, login_user, logout_user
from src.services.grader.provider_adapters import adapter_for_protocol
from src.services.grader.types import ProviderError
from src.services.model_registry import (
    create_model,
    delete_model,
    get_model,
    list_models,
    update_model,
    update_test_status,
)

admin_bp = Blueprint('admin', __name__, url_prefix='/api/admin')


def _write_model_log(current_user, action, model_id, detail, commit: bool = True):
    get_db().execute(
        """INSERT INTO admin_logs
           (admin_uid, action, target_type, target_id, detail)
           VALUES (?, ?, 'llm_model', ?, ?)""",
        (
            current_user["uid"],
            action,
            model_id,
            json.dumps(detail, ensure_ascii=False),
        ),
    )
    if commit:
        get_db().commit()


@admin_bp.route('/models', methods=['GET'])
@admin_required()
def get_llm_models(current_user):
    return api_success({
        "models": list_models(),
        "can_write": current_user.get("role") == "super_admin",
    })


@admin_bp.route('/models', methods=['POST'])
@admin_required('*')
def create_llm_model(current_user):
    data = request.get_json(silent=True)
    if not data:
        return api_error("Model configuration is required", 400)
    try:
        model_id = create_model(data, commit=False)
        model = get_model(model_id)
    except (TypeError, ValueError, RuntimeError) as exc:
        get_db().rollback()
        return api_error(str(exc), 400)
    try:
        _write_model_log(
            current_user,
            "create_llm_model",
            model_id,
            {"protocol": model["protocol"], "model_name": model["model_name"]},
            commit=False,
        )
        get_db().commit()
    except Exception:
        get_db().rollback()
        return api_error("Unable to save model", 500)
    return api_success(model)


@admin_bp.route('/models/<model_id>', methods=['PUT'])
@admin_required('*')
def update_llm_model(model_id, current_user):
    data = request.get_json(silent=True)
    if not data:
        return api_error("Model configuration is required", 400)
    if not get_model(model_id):
        return api_error("Model not found", 404)
    try:
        update_model(model_id, data, commit=False)
        model = get_model(model_id)
    except (TypeError, ValueError, RuntimeError) as exc:
        get_db().rollback()
        return api_error(str(exc), 400)
    changed_fields = sorted(field for field in data if field != "api_key")
    try:
        _write_model_log(
            current_user,
            "update_llm_model",
            model_id,
            {
                "changed_fields": changed_fields,
                "credential_replaced": bool(str(data.get("api_key", "")).strip()),
            },
            commit=False,
        )
        get_db().commit()
    except Exception:
        get_db().rollback()
        return api_error("Unable to save model", 500)
    return api_success(model)


@admin_bp.route('/models/<model_id>', methods=['DELETE'])
@admin_required('*')
def delete_llm_model(model_id, current_user):
    model = get_model(model_id)
    if not model:
        return api_error("Model not found", 404)
    try:
        disposition = delete_model(model_id, commit=False)
        _write_model_log(
            current_user,
            "delete_llm_model",
            model_id,
            {
                "protocol": model["protocol"],
                "model_name": model["model_name"],
                "disposition": disposition,
            },
            commit=False,
        )
        get_db().commit()
    except Exception:
        get_db().rollback()
        return api_error("Unable to delete model", 500)
    return api_success({"model_id": model_id, "disposition": disposition})


@admin_bp.route('/models/<model_id>/test', methods=['POST'])
@admin_required('*')
def test_llm_model(model_id, current_user):
    model = get_model(model_id, include_secret=True)
    if not model:
        return api_error("Model not found", 404)

    started = time.monotonic()
    success = False
    error_category = None
    provider_latency = None
    try:
        response = adapter_for_protocol(model["protocol"]).complete(
            [{"role": "user", "content": "Reply with OK."}],
            model,
        )
        success = True
        provider_latency = response.latency_ms
    except ProviderError as exc:
        error_category = exc.code
    except Exception:
        error_category = "internal"

    latency_ms = provider_latency
    if latency_ms is None:
        latency_ms = int((time.monotonic() - started) * 1000)
    status = "success" if success else "failure"
    result = {
        "success": success,
        "model_id": model_id,
        "name": model["name"],
        "protocol": model["protocol"],
        "error_category": error_category,
        "latency_ms": latency_ms,
    }
    try:
        update_test_status(
            model_id,
            status,
            error_category,
            latency_ms,
            commit=False,
        )
        _write_model_log(
            current_user,
            "test_llm_model",
            model_id,
            result,
            commit=False,
        )
        get_db().commit()
    except Exception:
        get_db().rollback()
        return api_error("Unable to record connection test", 500)
    return api_success(result)


@admin_bp.route('/feature-models', methods=['GET'])
@admin_required()
def get_feature_models(current_user):
    """列出各功能的模型绑定情况，及可供选择的模型列表。"""
    from src.services.feature_model_service import list_feature_bindings
    try:
        bindings = list_feature_bindings()
    except Exception as exc:
        return api_error(f"读取功能模型配置失败: {exc}", 500)
    return api_success({
        "features": bindings,
        "models": list_models(),
        "can_write": current_user.get("role") == "super_admin",
    })


@admin_bp.route('/feature-models/<feature_key>', methods=['PUT'])
@admin_required('*')
def update_feature_model(feature_key, current_user):
    """绑定或解绑某个功能使用的模型。

    body: {"model_id": "model_xxx"} 绑定；{"model_id": null} 或 "" 解绑（回退全局配置）。
    """
    from src.services.feature_model_service import set_feature_model
    data = request.get_json(silent=True) or {}
    model_id = data.get("model_id")
    try:
        set_feature_model(feature_key, model_id or "")
    except ValueError as exc:
        return api_error(str(exc), 400)
    return api_success({"feature_key": feature_key, "model_id": model_id or None})


@admin_bp.route('/auth/login', methods=['POST'])
def admin_login():
    """管理员登录"""
    data = request.get_json()
    if not data:
        return api_error("请提供登录信息", 400)

    username = data.get('username', '').strip()
    password = data.get('password', '')
    if not username or not password:
        return api_error("请输入用户名和密码", 400)

    result, error = login_user(username, password)
    if error:
        return api_error(error, 401)

    if result.get('role') not in ('super_admin', 'admin', 'reviewer', 'operator'):
        return api_error("无管理权限", 403)

    # Keep a compact session payload so page guards survive reloads.
    session.permanent = True
    session['admin_user'] = {
        'uid': result['uid'],
        'username': result['username'],
        'nickname': result.get('nickname'),
        'role': result['role'],
        'token': result['token'],
    }
    return api_success({
        'token': result['token'],
        'user': {
            'uid': result['uid'],
            'username': result['username'],
            'nickname': result.get('nickname'),
            'role': result['role'],
            'is_admin': True,
        }
    })


@admin_bp.route('/auth/verify', methods=['GET'])
@admin_bp.route('/auth/me', methods=['GET'])
@admin_required()
def admin_verify(current_user):
    """Verify current admin session/token for SPA bootstrapping."""
    return api_success({
        'is_admin': True,
        'uid': current_user.get('uid'),
        'username': current_user.get('username'),
        'nickname': current_user.get('nickname'),
        'role': current_user.get('role'),
    })


@admin_bp.route('/auth/logout', methods=['POST'])
def admin_logout():
    session_user = session.pop('admin_user', None)
    token = request.headers.get('Authorization', '').replace('Bearer ', '').strip()
    if token:
        user, _ = _extract_user_from_token()
        if user:
            logout_user(user['uid'], token=token)
    elif isinstance(session_user, dict) and session_user.get('token'):
        logout_user(session_user.get('uid'), token=session_user['token'])
    return api_success(message="Logged out")


def get_dashboard_stats():
    db = get_db()

    # User stats
    total_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    today_users = db.execute(
        "SELECT COUNT(*) FROM users WHERE date(created_at) = date('now')"
    ).fetchone()[0]
    vip_users = db.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'vip' OR vip_expire > datetime('now')"
    ).fetchone()[0]

    # Submission stats
    total_submissions = db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
    today_submissions = db.execute(
        "SELECT COUNT(*) FROM submissions WHERE date(created_at) = date('now')"
    ).fetchone()[0]

    # Pending review
    pending_reviews = db.execute(
        "SELECT COUNT(*) FROM submissions WHERE score < 60 OR is_reviewed = 0"
    ).fetchone()[0]

    # Papers stats
    total_papers = db.execute("SELECT COUNT(*) FROM papers").fetchone()[0]

    # Pending phrases
    pending_phrases = db.execute(
        "SELECT COUNT(*) FROM good_phrases WHERE status = 'pending'"
    ).fetchone()[0]

    # User growth (last 7 days)
    user_growth = []
    rows = db.execute(
        """SELECT date(created_at) as date, COUNT(*) as count
           FROM users WHERE created_at >= date('now', '-6 days')
           GROUP BY date(created_at) ORDER BY date"""
    ).fetchall()
    growth_map = {r['date']: r['count'] for r in rows}
    for i in range(6, -1, -1):
        d = db.execute(f"SELECT date('now', '-{i} days') as d").fetchone()['d']
        user_growth.append({'date': d, 'count': growth_map.get(d, 0)})

    # Score distribution
    low = db.execute("SELECT COUNT(*) FROM submissions WHERE score IS NOT NULL AND score < 60").fetchone()[0]
    medium = db.execute("SELECT COUNT(*) FROM submissions WHERE score >= 60 AND score < 75").fetchone()[0]
    high = db.execute("SELECT COUNT(*) FROM submissions WHERE score >= 75 AND score < 90").fetchone()[0]
    excellent = db.execute("SELECT COUNT(*) FROM submissions WHERE score >= 90").fetchone()[0]
    score_distribution = {'low': low, 'medium': medium, 'high': high, 'excellent': excellent}

    # Daily stats (last 7 days)
    daily_stats = []
    for i in range(6, -1, -1):
        d = db.execute(f"SELECT date('now', '-{i} days') as d").fetchone()['d']
        new_u = db.execute("SELECT COUNT(*) FROM users WHERE date(created_at) = ?", (d,)).fetchone()[0]
        active_u = db.execute("SELECT COUNT(*) FROM users WHERE date(last_login) = ?", (d,)).fetchone()[0]
        new_s = db.execute("SELECT COUNT(*) FROM submissions WHERE date(created_at) = ?", (d,)).fetchone()[0]
        new_v = db.execute("SELECT COUNT(*) FROM users WHERE date(created_at) = ? AND (role = 'vip' OR vip_expire IS NOT NULL)", (d,)).fetchone()[0]
        daily_stats.append({'date': d, 'new_users': new_u, 'active_users': active_u, 'new_submissions': new_s, 'new_vip': new_v})

    # Hot papers (top 10)
    hot_papers = [dict(r) for r in db.execute(
        """SELECT p.title, p.exam_type, p.year, COUNT(s.sid) as submission_count,
                  AVG(s.score) as avg_score
           FROM papers p
           LEFT JOIN submissions s ON p.pid = s.pid
           GROUP BY p.pid ORDER BY submission_count DESC LIMIT 10"""
    ).fetchall()]

    return {
        'total_users': total_users,
        'today_users': today_users,
        'vip_users': vip_users,
        'total_submissions': total_submissions,
        'today_submissions': today_submissions,
        'pending_reviews': pending_reviews,
        'total_papers': total_papers,
        'pending_phrases': pending_phrases,
        'user_growth': user_growth,
        'score_distribution': score_distribution,
        'daily_stats': daily_stats,
        'hot_papers': hot_papers
    }


@admin_bp.route('/dashboard', methods=['GET'])
@admin_required('stats.view')
def dashboard():
    return api_success(get_dashboard_stats())


@admin_bp.route('/stats', methods=['GET'])
@admin_required('stats.view')
def stats():
    return api_success(get_dashboard_stats())


@admin_bp.route('/token-usage', methods=['GET'])
@admin_required('stats.view')
def token_usage():
    """近 N 天 token 消耗统计"""
    from src.services import token_usage_service
    days = request.args.get('days', 7, type=int)
    days = max(1, min(days, 90))
    summary = token_usage_service.get_usage_summary(days)
    return api_success(summary)


@admin_bp.route('/token-usage/records', methods=['GET'])
@admin_required('stats.view')
def token_usage_records():
    """每次 LLM 调用的 token 消耗明细（分页）"""
    from src.services import token_usage_service
    page = request.args.get('page', 1, type=int)
    per_page = clamp_per_page(request.args.get('limit', 20, type=int), max_val=200)
    source = request.args.get('source', '').strip()
    model_name = request.args.get('model_name', '').strip()
    result = token_usage_service.get_usage_records(
        page=page, per_page=per_page, source=source or None,
        model_name=model_name or None,
    )
    return api_success(result)


# ============ User Management ============

@admin_bp.route('/users', methods=['GET'])
@admin_required('users.view')
def list_users():
    page = request.args.get('page', 1, type=int)
    per_page = clamp_per_page(request.args.get('limit', 20, type=int))
    role = request.args.get('role')
    status = request.args.get('status')
    search = request.args.get('search', '')[:100]  # Limit search length

    db = get_db()
    where_clause = " WHERE 1=1"
    params = []

    if role:
        where_clause += " AND role = ?"
        params.append(role)
    if status:
        where_clause += " AND status = ?"
        params.append(status)
    if search:
        where_clause += " AND (username LIKE ? OR nickname LIKE ?)"
        params.append(f'%{search}%')
        params.append(f'%{search}%')

    total = db.execute(f"SELECT COUNT(*) FROM users{where_clause}", params).fetchone()[0]

    offset = (page - 1) * per_page
    query = f"SELECT uid, username, nickname, role, grading_credits, vip_expire, created_at, last_login, status FROM users{where_clause} ORDER BY created_at DESC LIMIT {per_page} OFFSET {offset}"
    users = db.execute(query, params).fetchall()

    return api_success({
        'users': [dict(u) for u in users],
        'total': total,
        'page': page,
        'pages': (total + per_page - 1) // per_page
    })


@admin_bp.route('/users/<uid>', methods=['GET'])
@admin_required('users.view')
def get_user(uid):
    user = get_user_profile(uid)
    if not user:
        return api_error("用户不存在", 404)

    # Get learning stats
    db = get_db()
    submissions_count = db.execute(
        "SELECT COUNT(*) FROM submissions WHERE uid = ?", (uid,)
    ).fetchone()[0]
    avg_score = db.execute(
        "SELECT AVG(score) FROM submissions WHERE uid = ? AND score IS NOT NULL", (uid,)
    ).fetchone()[0]

    user['submissions_count'] = submissions_count
    user['avg_score'] = round(avg_score, 1) if avg_score else None
    user['is_vip'] = is_vip_user(user)

    return api_success(user)


@admin_bp.route('/users/<uid>', methods=['PUT'])
@admin_required('users.edit')
def update_user(uid):
    data = request.get_json()
    if not data:
        return api_error("请提供更新数据", 400)

    # Validate role assignment (H2)
    valid_roles = ('user', 'vip', 'admin', 'super_admin', 'reviewer', 'operator')
    if 'role' in data and data['role'] not in valid_roles:
        return api_error(f"无效角色，可选: {', '.join(valid_roles)}", 400)

    db = get_db()
    fields = []
    values = []

    for field in ['nickname', 'role', 'vip_expire', 'status']:
        if field in data:
            fields.append(f"{field} = ?")
            values.append(data[field])

    if fields:
        values.append(uid)
        db.execute(f"UPDATE users SET {', '.join(fields)} WHERE uid = ?", values)
        db.commit()

    # Log action with actual admin UID (H3)
    admin_uid = session.get('admin_user', {}).get('uid', 'system')
    db.execute(
        "INSERT INTO admin_logs (admin_uid, action, target_type, target_id, detail) VALUES (?, 'update_user', 'user', ?, ?)",
        (admin_uid, uid, json.dumps(data))
    )
    db.commit()

    return api_success(message="用户已更新")


@admin_bp.route('/users/<uid>/ban', methods=['PUT'])
@admin_required('users.ban')
def ban_user(uid):
    data = request.get_json() or {}
    action = data.get('action', 'ban')

    db = get_db()
    new_status = 'banned' if action == 'ban' else 'active'
    db.execute("UPDATE users SET status = ? WHERE uid = ?", (new_status, uid))
    db.commit()

    admin_uid = session.get('admin_user', {}).get('uid', 'system')
    db.execute(
        "INSERT INTO admin_logs (admin_uid, action, target_type, target_id, detail) VALUES (?, ?, 'user', ?, ?)",
        (admin_uid, action + '_user', uid, json.dumps({'action': action}))
    )
    db.commit()

    return api_success(message=f"用户已{'封禁' if action == 'ban' else '解封'}")


@admin_bp.route('/users/<uid>/credits', methods=['PUT'])
@admin_required('*')
def update_user_credits(uid):
    data = request.get_json() or {}
    credits = data.get('credits')
    if credits is None:
        return api_error("请指定批改次数", 400)
    try:
        credits = float(credits)
    except (TypeError, ValueError):
        return api_error("批改次数必须为数字", 400)

    db = get_db()
    db.execute("UPDATE users SET grading_credits = ? WHERE uid = ?", (credits, uid))
    db.commit()

    # Log transaction
    from src.services import exchange_code_service
    exchange_code_service._log_transaction(uid, "grant", credits, credits,
                                            f"管理员调整批改次数为 {credits}", commit=True)

    return api_success({
        "message": f"已更新为 {credits} 次",
        "credits": credits,
    })


# ============ Paper Management ============

@admin_bp.route('/papers', methods=['GET'])
@admin_required('papers.view')
def list_papers():
    page = request.args.get('page', 1, type=int)
    per_page = clamp_per_page(request.args.get('limit', 20, type=int))
    status = request.args.get('status')
    exam_type = request.args.get('exam_type')

    db = get_db()
    query = "SELECT * FROM papers WHERE 1=1"
    params = []

    if status:
        query += " AND status = ?"
        params.append(status)
    if exam_type:
        query += " AND exam_type = ?"
        params.append(exam_type)

    total = db.execute(
        query.replace("SELECT *", "SELECT COUNT(*)"), params
    ).fetchone()[0]

    offset = (page - 1) * per_page
    query += f" ORDER BY created_at DESC LIMIT {per_page} OFFSET {offset}"
    papers = db.execute(query, params).fetchall()

    return api_success({
        'papers': [dict(p) for p in papers],
        'total': total,
        'page': page,
        'pages': (total + per_page - 1) // per_page
    })


@admin_bp.route('/papers', methods=['POST'])
@admin_required('papers.add')
def create_paper():
    data = request.get_json()
    if not data:
        return api_error("请提供试卷数据", 400)

    pid = paper_service.create_paper(data)
    return api_success({'pid': pid}, "试卷已创建")


@admin_bp.route('/papers/<pid>', methods=['PUT'])
@admin_required('papers.edit')
def update_paper(pid):
    data = request.get_json()
    if not data:
        return api_error("请提供更新数据", 400)

    paper_service.update_paper(pid, data)
    return api_success(message="试卷已更新")


@admin_bp.route('/papers/<pid>', methods=['DELETE'])
@admin_required('papers.delete')
def delete_paper(pid):
    paper_service.delete_paper(pid)
    return api_success(message="试卷已删除")


@admin_bp.route('/papers/<pid>/publish', methods=['PUT'])
@admin_required('papers.edit')
def publish_paper(pid):
    data = request.get_json()
    status = data.get('status', 'published') if data else 'published'

    paper_service.update_paper(pid, {'status': status})
    return api_success(message=f"试卷已{'发布' if status == 'published' else '下架'}")


# ============ Phrase Management ============

@admin_bp.route('/phrases', methods=['POST'])
@admin_required('phrases.add')
def add_phrase():
    data = request.get_json()
    if not data or not data.get('phrase') or not data.get('source'):
        return api_error("请提供好词内容和来源", 400)

    phrase_id = phrase_service.add_phrase({
        'phrase': data['phrase'],
        'translation': data.get('translation'),
        'usage': data.get('usage'),
        'source': data['source'],
        'source_url': data.get('source_url'),
        'source_date': data.get('source_date'),
        'tag': data.get('tag', [])
    })

    # Auto-approve if created by admin
    if data.get('status') == 'approved':
        phrase_service.approve_phrase(phrase_id, session.get('admin_user', {}).get('uid', 'admin'))

    return api_success({'id': phrase_id}, "好词已添加")


@admin_bp.route('/phrases', methods=['GET'])
@admin_required('phrases.view')
def list_phrases():
    page = request.args.get('page', 1, type=int)
    per_page = clamp_per_page(request.args.get('limit', 20, type=int))
    status = request.args.get('status')
    source = request.args.get('source')

    db = get_db()
    query = "SELECT * FROM good_phrases WHERE 1=1"
    params = []

    if status:
        query += " AND status = ?"
        params.append(status)
    if source:
        query += " AND source = ?"
        params.append(source)

    total = db.execute(
        query.replace("SELECT *", "SELECT COUNT(*)"), params
    ).fetchone()[0]

    offset = (page - 1) * per_page
    query += f" ORDER BY created_at DESC LIMIT {per_page} OFFSET {offset}"
    phrases = db.execute(query, params).fetchall()

    return api_success({
        'phrases': [dict(p) for p in phrases],
        'total': total,
        'page': page,
        'pages': (total + per_page - 1) // per_page
    })


@admin_bp.route('/phrases/<int:phrase_id>/approve', methods=['POST'])
@admin_required('phrases.approve')
def approve_phrase(phrase_id):
    admin_uid = session.get('admin_user', {}).get('uid', 'system')
    phrase_service.approve_phrase(phrase_id, admin_uid)
    return api_success(message="好词已审核通过")


@admin_bp.route('/phrases/<int:phrase_id>/reject', methods=['POST'])
@admin_required('phrases.approve')
def reject_phrase(phrase_id):
    phrase_service.reject_phrase(phrase_id)
    return api_success(message="好词已驳回")


# ============ Review Management ============

@admin_bp.route('/submissions/pending_review', methods=['GET'])
@admin_required('submissions.review')
def pending_reviews():
    db = get_db()
    submissions = db.execute(
        """SELECT s.*, u.nickname, p.title as paper_title
           FROM submissions s
           JOIN users u ON s.uid = u.uid
           JOIN papers p ON s.pid = p.pid
           WHERE (s.score < 60 OR s.needs_review = 1) AND s.is_reviewed = 0
           ORDER BY s.created_at DESC LIMIT 50"""
    ).fetchall()

    return api_success({
        'submissions': [dict(s) for s in submissions]
    })


@admin_bp.route('/submissions/<sid>/review', methods=['POST'])
@admin_required('submissions.review')
def review_submission(sid):
    data = request.get_json()
    if not data:
        return api_error("请提供复核结果", 400)

    db = get_db()
    db.execute(
        """UPDATE submissions SET
           score = ?, dimension_scores = ?, ai_feedback = ?,
           is_reviewed = 1, needs_review = 0
           WHERE sid = ?""",
        (
            data.get('score'),
            json.dumps(data.get('dimension_scores', {})),
            data.get('feedback'),
            sid
        )
    )
    db.commit()

    return api_success(message="复核已完成")


# ============ Logs ============

@admin_bp.route('/logs', methods=['GET'])
@admin_required('logs.view')
def get_logs():
    page = request.args.get('page', 1, type=int)
    per_page = clamp_per_page(request.args.get('limit', 50, type=int))
    action = request.args.get('action')
    admin = request.args.get('admin')
    start = request.args.get('start')
    end = request.args.get('end')

    db = get_db()
    query = "SELECT * FROM admin_logs WHERE 1=1"
    params = []

    if action:
        query += " AND action LIKE ?"
        params.append(f'%{action}%')
    if admin:
        query += " AND (admin_uid LIKE ? OR admin_uid = ?)"
        params.append(f'%{admin}%')
        params.append(admin)
    if start:
        query += " AND date(created_at) >= ?"
        params.append(start)
    if end:
        query += " AND date(created_at) <= ?"
        params.append(end)

    total = db.execute(
        "SELECT COUNT(*) FROM admin_logs WHERE 1=1" +
        (" AND action LIKE ?" if action else "") +
        (" AND (admin_uid LIKE ? OR admin_uid = ?)" if admin else "") +
        (" AND date(created_at) >= ?" if start else "") +
        (" AND date(created_at) <= ?" if end else ""),
        params
    ).fetchone()[0]

    offset = (page - 1) * per_page
    query += f" ORDER BY created_at DESC LIMIT {per_page} OFFSET {offset}"
    logs = db.execute(query, params).fetchall()

    return api_success({
        'logs': [dict(l) for l in logs],
        'total': total,
        'page': page,
        'pages': (total + per_page - 1) // per_page
    })


# ============ Settings ============

DEFAULT_SETTINGS = {
    'site_name': '申论帮',
    'site_description': 'AI驱动的申论批改平台',
    'contact_email': '',
    'free_grades': 3,
    'vip_daily_grades': 10,
    'ai_review_enabled': True,
    'low_score_threshold': 60,
    'free_trial_days': 30,
    'monthly_price': 99,
    'yearly_price': 399,
    'credit_ratio': 0.1,
    'llm_provider': 'deepseek',
    'llm_model': 'deepseek-chat',
    'llm_base_url': 'https://api.deepseek.com'
}


@admin_bp.route('/settings', methods=['GET'])
@admin_required('stats.view')
def get_settings():
    db = get_db()
    rows = db.execute("SELECT key, value FROM settings").fetchall()
    settings = dict(DEFAULT_SETTINGS)
    for row in rows:
        try:
            settings[row['key']] = json.loads(row['value'])
        except (json.JSONDecodeError, TypeError):
            settings[row['key']] = row['value']
    return api_success(settings)


@admin_bp.route('/settings', methods=['PUT'])
@admin_required('*')
def update_settings():
    data = request.get_json()
    if not data:
        return api_error("请提供设置数据", 400)

    # Validate settings keys - only allow known keys
    allowed_keys = set(DEFAULT_SETTINGS.keys())
    invalid_keys = set(data.keys()) - allowed_keys
    if invalid_keys:
        return api_error(f"不支持的设置项: {', '.join(invalid_keys)}", 400)

    db = get_db()
    for key, value in data.items():
        db.execute(
            """INSERT INTO settings (key, value, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = datetime('now')""",
            (key, json.dumps(value, ensure_ascii=False), json.dumps(value, ensure_ascii=False))
        )
    db.commit()

    # Invalidate LLM cache if LLM settings changed
    llm_keys = {'llm_provider', 'llm_model', 'llm_api_key', 'llm_base_url'}
    if llm_keys & set(data.keys()):
        from src.config import invalidate_llm_cache
        invalidate_llm_cache()

    db.execute(
        "INSERT INTO admin_logs (admin_uid, action, target_type, detail) VALUES (?, 'update_settings', 'system', ?)",
        (session.get('admin_user', {}).get('uid', 'admin'), json.dumps(list(data.keys())))
    )
    db.commit()

    return api_success(message="设置已保存")


@admin_bp.route('/settings/test-llm', methods=['POST'])
@admin_required('*')
def test_llm():
    """测试 LLM 连通性"""
    from src.config import get_llm_config
    import requests as req

    llm = get_llm_config()
    try:
        resp = req.post(
            f"{llm['base_url']}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {llm['api_key']}"
            },
            json={
                "model": llm['model'],
                "messages": [{"role": "user", "content": "回复ok"}],
                "max_tokens": 10
            },
            timeout=15
        )
        resp.raise_for_status()
        result = resp.json()
        content = result['choices'][0]['message']['content']
        return api_success({
            'status': 'ok',
            'provider': llm['provider'],
            'model': llm['model'],
            'response': content[:100]
        })
    except Exception as e:
        return api_error(f"LLM 连接失败: {str(e)}", 500)


@admin_bp.route('/settings/cache', methods=['DELETE'])
@admin_required('*')
def clear_cache():
    try:
        from src.services.grader.cache import grader_cache
        if grader_cache.client:
            grader_cache.client.flushdb()
        return api_success(message="缓存已清除")
    except Exception as e:
        return api_error("清除缓存失败", 500)

