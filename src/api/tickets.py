"""客服工单（用户端）：提交建议/反馈、查看管理员回复、追问、关闭。

管理员回复与工单管理在管理后台 /api/admin/tickets（见 admin.py）。
"""
import uuid
from datetime import datetime, timezone

from flask import Blueprint, request

from src.api.utils import (
    api_error,
    api_success,
    clamp_per_page,
    get_db,
    optional_token,
    token_required,
)

tickets_bp = Blueprint('tickets', __name__, url_prefix='/api/tickets')

CATEGORIES = ('建议', '问题反馈', '投诉', '其他')
STATUS_LABELS = {'open': '待处理', 'replied': '已回复', 'closed': '已关闭'}


def _new_ticket_no() -> str:
    now = datetime.now(timezone.utc)
    return 'TK' + now.strftime('%Y%m%d') + '-' + uuid.uuid4().hex[:4].upper()


def _now() -> str:
    # 与 SQLite datetime('now') 一致的 UTC 字符串，保证未读时间比较正确
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _get_ticket(ticket_id: int) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
    ).fetchone()
    return dict(row) if row else None


def _format_ticket(row: dict) -> dict:
    """工单展示字段（含未读新回复标记）。"""
    row = dict(row)
    row['status_label'] = STATUS_LABELS.get(row.get('status'), row.get('status'))
    last_admin = row.get('last_admin_reply_at')
    read_at = row.get('user_read_at')
    row['has_new_reply'] = bool(
        last_admin
        and row.get('status') != 'closed'
        and (not read_at or str(last_admin) > str(read_at))
    )
    return row


@tickets_bp.route('', methods=['GET'])
@optional_token
def list_tickets(current_user):
    """我的工单列表（分页）。"""
    if not current_user:
        return api_success({'tickets': [], 'total': 0, 'page': 1, 'pages': 0})
    page = max(1, request.args.get('page', 1, type=int))
    per_page = clamp_per_page(request.args.get('limit', 10, type=int))
    db = get_db()
    total = db.execute(
        "SELECT COUNT(*) FROM tickets WHERE uid = ?", (current_user['uid'],)
    ).fetchone()[0]
    rows = db.execute(
        """SELECT * FROM tickets WHERE uid = ?
           ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?""",
        (current_user['uid'], per_page, (page - 1) * per_page),
    ).fetchall()
    return api_success({
        'tickets': [_format_ticket(dict(r)) for r in rows],
        'total': total,
        'page': page,
        'pages': (total + per_page - 1) // per_page,
    })


@tickets_bp.route('', methods=['POST'])
@token_required
def create_ticket(current_user):
    """提交工单（建议/反馈）。"""
    data = request.get_json(silent=True) or {}
    category = str(data.get('category') or '建议').strip()
    title = str(data.get('title') or '').strip()
    content = str(data.get('content') or '').strip()
    if category not in CATEGORIES:
        category = '建议'
    if not title:
        return api_error('请填写工单标题', 400)
    if not content:
        return api_error('请填写工单内容', 400)
    if len(title) > 100:
        return api_error('标题不能超过100字', 400)
    if len(content) > 5000:
        return api_error('内容不能超过5000字', 400)

    db = get_db()
    ticket_no = _new_ticket_no()
    cur = db.execute(
        """INSERT INTO tickets (ticket_no, uid, category, title, content, status, updated_at)
           VALUES (?, ?, ?, ?, ?, 'open', ?)""",
        (ticket_no, current_user['uid'], category, title, content, _now()),
    )
    db.execute(
        """INSERT INTO ticket_replies (ticket_id, author_uid, author_role, content, is_system)
           VALUES (?, '', 'system', ?, 1)""",
        (cur.lastrowid, '工单已提交，我们会尽快处理，请留意管理员回复。'),
    )
    db.commit()
    return api_success({'id': cur.lastrowid, 'ticket_no': ticket_no}, '工单已提交')


@tickets_bp.route('/<int:ticket_id>', methods=['GET'])
@token_required
def get_ticket(current_user, ticket_id):
    """工单详情 + 回复串（本人可见，打开即标记已读）。"""
    ticket = _get_ticket(ticket_id)
    if not ticket or ticket['uid'] != current_user['uid']:
        return api_error('工单不存在', 404)
    db = get_db()
    replies = db.execute(
        """SELECT author_role, content, is_system, created_at FROM ticket_replies
           WHERE ticket_id = ? ORDER BY id ASC""",
        (ticket_id,),
    ).fetchall()
    result = _format_ticket(ticket)
    result['replies'] = [dict(r) for r in replies]
    db.execute(
        "UPDATE tickets SET user_read_at = ? WHERE id = ?", (_now(), ticket_id)
    )
    db.commit()
    return api_success(result)


@tickets_bp.route('/<int:ticket_id>/reply', methods=['POST'])
@token_required
def reply_ticket(current_user, ticket_id):
    """用户追问（关闭后不可回复，回复后回到待处理）。"""
    ticket = _get_ticket(ticket_id)
    if not ticket or ticket['uid'] != current_user['uid']:
        return api_error('工单不存在', 404)
    if ticket['status'] == 'closed':
        return api_error('工单已关闭，无法回复', 400)
    content = str((request.get_json(silent=True) or {}).get('content') or '').strip()
    if not content:
        return api_error('回复内容不能为空', 400)
    if len(content) > 5000:
        return api_error('回复不能超过5000字', 400)

    db = get_db()
    now = _now()
    db.execute(
        """INSERT INTO ticket_replies (ticket_id, author_uid, author_role, content)
           VALUES (?, ?, 'user', ?)""",
        (ticket_id, current_user['uid'], content),
    )
    db.execute(
        """UPDATE tickets SET status = 'open', updated_at = ?, user_read_at = ? WHERE id = ?""",
        (now, now, ticket_id),
    )
    db.commit()
    return api_success(message='回复成功')


@tickets_bp.route('/<int:ticket_id>/close', methods=['POST'])
@token_required
def close_ticket(current_user, ticket_id):
    """用户关闭工单。"""
    ticket = _get_ticket(ticket_id)
    if not ticket or ticket['uid'] != current_user['uid']:
        return api_error('工单不存在', 404)
    now = _now()
    get_db().execute(
        """UPDATE tickets SET status = 'closed', updated_at = ?, user_read_at = ? WHERE id = ?""",
        (now, now, ticket_id),
    )
    get_db().commit()
    return api_success(message='工单已关闭')


@tickets_bp.route('/unread-count', methods=['GET'])
@optional_token
def unread_count(current_user):
    """未读新回复数（导航徽标）。"""
    if not current_user:
        return api_success({'count': 0})
    row = get_db().execute(
        """SELECT COUNT(*) AS c FROM tickets
           WHERE uid = ? AND status != 'closed'
             AND last_admin_reply_at IS NOT NULL
             AND (user_read_at IS NULL OR last_admin_reply_at > user_read_at)""",
        (current_user['uid'],),
    ).fetchone()
    return api_success({'count': row['c']})
