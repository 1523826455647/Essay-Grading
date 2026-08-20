"""
兑换码 API - 用户兑换 + 管理员管理
"""

from flask import Blueprint, request
from src.api.utils import api_success, api_error, token_required, admin_required, get_db
from src.services import exchange_code_service

codes_bp = Blueprint('codes', __name__, url_prefix='/api')


# ==================== 用户端：兑换码 ====================

@codes_bp.route('/codes/redeem', methods=['POST'])
@token_required
def redeem_code(current_user):
    data = request.get_json(silent=True)
    if not data or not data.get('code'):
        return api_error("请输入兑换码", 400)

    code = data['code'].strip().upper()
    result = exchange_code_service.redeem_code(current_user['uid'], code)

    if result['success']:
        return api_success(result)
    return api_error(result['message'], 400)


@codes_bp.route('/user/credits', methods=['GET'])
@token_required
def get_credits(current_user):
    from src.services import package_service
    balance = exchange_code_service.get_user_credits(current_user['uid'])
    pkg_info = package_service.get_user_package_balance(current_user['uid'])
    return api_success({"credits": balance, "package": pkg_info})


@codes_bp.route('/user/packages', methods=['GET'])
@token_required
def get_user_packages(current_user):
    """获取用户所有套餐详情（含历史记录）"""
    from src.services import package_service
    pkgs = package_service.get_user_packages(current_user['uid'])
    return api_success({"packages": pkgs})


# ==================== 管理员端：兑换码管理 ====================

@codes_bp.route('/admin/codes', methods=['GET'])
@admin_required()
def list_codes(current_user):
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('limit', 20, type=int)
    result = exchange_code_service.list_codes(page, per_page)
    return api_success(result)


@codes_bp.route('/admin/codes', methods=['POST'])
@admin_required('*')
def create_codes(current_user):
    data = request.get_json(silent=True)
    if not data:
        return api_error("请提供参数", 400)

    credits = float(data.get('credits', 5))
    count = int(data.get('count', 1))
    max_uses = int(data.get('max_uses', 1))
    prefix = data.get('prefix', 'SLB')
    expires_days = int(data.get('expires_days', 365))
    package_id = data.get('package_id')

    if count > 100:
        return api_error("单次最多生成 100 个兑换码", 400)

    # 如果关联套餐，credits 可以为 0（套餐自带次数）
    if not package_id and credits <= 0:
        return api_error("批改次数必须大于 0", 400)

    codes = exchange_code_service.batch_generate(
        credits=credits,
        count=count,
        max_uses=max_uses,
        prefix=prefix,
        created_by=current_user.get('uid', ''),
        expires_days=expires_days,
        package_id=package_id,
    )

    return api_success({
        "codes": codes,
        "count": len(codes),
        "credits_each": credits,
    })


@codes_bp.route('/admin/codes/<code>', methods=['DELETE'])
@admin_required('*')
def disable_code(current_user, code):
    exchange_code_service.disable_code(code.upper())
    return api_success({"message": "兑换码已禁用"})


# ==================== 用户端：交易记录 ====================

@codes_bp.route('/user/transactions', methods=['GET'])
@token_required
def get_transactions(current_user):
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('limit', 30, type=int), 50)
    offset = (page - 1) * per_page

    db = get_db()
    total = db.execute(
        "SELECT COUNT(*) FROM credit_transactions WHERE uid=?",
        (current_user['uid'],)
    ).fetchone()[0]

    rows = db.execute(
        """SELECT trans_type, amount, balance, detail, created_at
           FROM credit_transactions WHERE uid=?
           ORDER BY created_at DESC LIMIT ? OFFSET ?""",
        (current_user['uid'], per_page, offset)
    ).fetchall()

    type_labels = {"recharge": "充值", "consume": "批改消耗", "grant": "赠送"}
    transactions = []
    for r in rows:
        t = dict(r)
        t['type_label'] = type_labels.get(t['trans_type'], t['trans_type'])
        transactions.append(t)

    return api_success({
        "transactions": transactions,
        "total": total,
        "page": page,
        "pages": max(1, (total + per_page - 1) // per_page),
    })
