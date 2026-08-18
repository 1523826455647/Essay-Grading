"""
Subscription & payment API for market-ready commercialization.
"""
import uuid
from datetime import datetime, timedelta
from flask import Blueprint, request
from src.api.utils import api_success, api_error, token_required, admin_required, get_db

subs_bp = Blueprint('subscriptions', __name__, url_prefix='/api')

# Subscription plan definitions
PLANS = {
    'trial':   {'name': '体验包', 'credits': 3,   'price': 9.9,  'days': 7},
    'monthly': {'name': '月卡',   'credits': 20,  'price': 29.9, 'days': 30},
    'quarter': {'name': '季卡',   'credits': -1,  'price': 79.0, 'days': 90},   # -1 = unlimited
    'yearly':  {'name': '年卡',   'credits': -1,  'price': 199.0,'days': 365},
}

@subs_bp.route('/subscriptions/plans', methods=['GET'])
def get_plans():
    return api_success({"plans": PLANS})


@subs_bp.route('/subscriptions/buy', methods=['POST'])
@token_required
def buy_subscription(current_user):
    data = request.get_json(silent=True) or {}
    plan_id = data.get('plan_id', '').strip()
    if plan_id not in PLANS:
        return api_error('无效的套餐', 400)

    plan = PLANS[plan_id]
    db = get_db()
    uid = current_user['uid']

    # Check existing subscription
    existing = db.execute(
        "SELECT * FROM user_subscriptions WHERE uid=? AND expires_at > datetime('now') ORDER BY expires_at DESC LIMIT 1",
        (uid,)
    ).fetchone()

    if existing and plan_id in ('trial',):
        return api_error('每个用户仅限购买一次体验包', 400)

    # Create subscription order (payment would go here in production)
    order_id = 'SUB-' + uuid.uuid4().hex[:12].upper()
    now = datetime.now().isoformat()
    expires = (datetime.now() + timedelta(days=plan['days'])).isoformat()

    db.execute("""
        INSERT INTO user_subscriptions (uid, plan_id, order_id, credits_granted, 
        price_paid, status, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, 'paid', ?, ?)
    """, (uid, plan_id, order_id, plan['credits'], plan['price'], now, expires))

    # Add credits
    if plan['credits'] > 0:
        current = float(current_user.get('grading_credits') or 0)
        new_balance = round(current + plan['credits'], 1)
        db.execute("UPDATE users SET grading_credits=? WHERE uid=?", (new_balance, uid))
        db.execute("""
            INSERT INTO credit_transactions (uid, trans_type, amount, balance, detail, created_at)
            VALUES (?, 'recharge', ?, ?, ?, ?)
        """, (uid, plan['credits'], new_balance, f'订阅套餐: {plan["name"]}', now))
    elif plan['credits'] == -1:
        db.execute("UPDATE users SET grading_credits=9999 WHERE uid=?", (uid,))
        db.execute("""
            INSERT INTO credit_transactions (uid, trans_type, amount, balance, detail, created_at)
            VALUES (?, 'recharge', 9999, 9999, ?, ?)
        """, (uid, f'订阅套餐(不限次): {plan["name"]}', now))

    # Mark trial used
    if plan_id == 'trial':
        db.execute("UPDATE users SET free_trial_used=1 WHERE uid=?", (uid,))

    db.commit()

    return api_success({
        'order_id': order_id,
        'plan': plan['name'],
        'credits_added': plan['credits'] if plan['credits'] > 0 else 'unlimited',
        'expires_at': expires,
    })


@subs_bp.route('/daily-practice', methods=['GET'])
@token_required
def get_daily_practice(current_user):
    """Get today's daily practice question"""
    db = get_db()
    uid = current_user['uid']
    today = datetime.now().strftime('%Y-%m-%d')

    # Check if already done today
    done = db.execute(
        "SELECT sid FROM daily_practice WHERE uid=? AND practice_date=?",
        (uid, today)
    ).fetchone()

    if done:
        return api_success({"completed": True, "message": "今日已完成练习"})

    # Select a random question based on user's weak areas
    # First check weak areas
    recent = db.execute("""
        SELECT dim_name, avg_score FROM user_dimension_trends
        WHERE uid=? GROUP BY dim_name ORDER BY avg_score ASC LIMIT 2
    """, (uid,)).fetchall()

    weak_types = [r['dim_name'] for r in recent] if recent else []

    # Get a random question, prefer weak types
    q = db.execute("""
        SELECT p.pid, p.title, p.questions, p.exam_type
        FROM papers p WHERE p.status='published'
        ORDER BY RANDOM() LIMIT 1
    """).fetchone()

    if not q:
        return api_success({"completed": True, "message": "题库更新中，请稍后再来"})

    import json
    questions = json.loads(q['questions'])
    question = questions[0] if questions else {}
    question.update({
        'pid': q['pid'],
        'paper_title': q['title'],
    })

    return api_success({
        "completed": False,
        "question": question,
        "focus_areas": weak_types,
    })


@subs_bp.route('/daily-practice', methods=['POST'])
@token_required
def submit_daily_practice(current_user):
    data = request.get_json(silent=True) or {}
    uid = current_user['uid']
    today = datetime.now().strftime('%Y-%m-%d')

    db = get_db()
    db.execute("""
        INSERT OR REPLACE INTO daily_practice (uid, practice_date, pid, qid, score, dimension_scores, created_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
    """, (uid, today, data.get('pid',''), data.get('qid',''), 
          data.get('score'), str(data.get('dimension_scores',{}) or {})))

    # Update streak
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    yest = db.execute("SELECT streak FROM daily_practice_streaks WHERE uid=?", (uid,)).fetchone()
    streak = (yest['streak'] + 1 if yest and yest['streak'] else 0) if db.execute(
        "SELECT 1 FROM daily_practice WHERE uid=? AND practice_date=?", (uid, yesterday)
    ).fetchone() else 1

    db.execute("""
        INSERT OR REPLACE INTO daily_practice_streaks (uid, streak, last_practice)
        VALUES (?, ?, ?)
    """, (uid, streak, today))
    db.commit()

    return api_success({"streak": streak})


@subs_bp.route('/user/trends', methods=['GET'])
@token_required
def get_user_trends(current_user):
    """Get user score trends for dashboard"""
    uid = current_user['uid']
    db = get_db()

    # Score trend (last 30 days)
    rows = db.execute("""
        SELECT DATE(created_at) as date, AVG(score) as avg_score, COUNT(*) as cnt
        FROM submissions WHERE uid=? AND score IS NOT NULL
        AND created_at >= datetime('now', '-30 days')
        GROUP BY DATE(created_at) ORDER BY date
    """, (uid,)).fetchall()

    score_trend = [{'date': r['date'], 'score': round(r['avg_score'], 1), 'count': r['cnt']} for r in rows]

    # Dimension trends
    dim_rows = db.execute("""
        SELECT dim_name, AVG(avg_score) as avg FROM user_dimension_trends
        WHERE uid=? GROUP BY dim_name
    """, (uid,)).fetchall()
    dim_trend = [{'name': r['dim_name'], 'score': round(r['avg'], 1)} for r in dim_rows]

    # Streak
    streak_row = db.execute("SELECT streak FROM daily_practice_streaks WHERE uid=?", (uid,)).fetchone()
    streak = streak_row['streak'] if streak_row else 0

    # Weak points
    weak = db.execute("""
        SELECT missing_point, COUNT(*) as cnt FROM user_weak_points
        WHERE uid=? GROUP BY missing_point ORDER BY cnt DESC LIMIT 5
    """, (uid,)).fetchall()

    return api_success({
        'score_trend': score_trend,
        'dimension_trends': dim_trend,
        'streak': streak,
        'weak_points': [{'point': r['missing_point'], 'count': r['cnt']} for r in weak],
    })


# Admin analytics
@subs_bp.route('/admin/analytics', methods=['GET'])
@admin_required()
def get_analytics(current_user):
    db = get_db()

    # Key metrics
    total_users = db.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0]
    total_submissions = db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
    total_revenue = db.execute("SELECT COALESCE(SUM(price_paid),0) FROM user_subscriptions WHERE status='paid'").fetchone()[0]
    active_today = db.execute(
        "SELECT COUNT(DISTINCT uid) FROM submissions WHERE DATE(created_at)=DATE('now')"
    ).fetchone()[0]

    # Model performance
    model_stats = db.execute("""
        SELECT lm.name, COUNT(*) as total, 
               SUM(CASE WHEN sj.status='completed' THEN 1 ELSE 0 END) as success,
               ROUND(AVG(sj.latency_ms),0) as avg_latency
        FROM submission_judgments sj
        JOIN llm_models lm ON sj.model_id=lm.model_id
        WHERE sj.created_at >= datetime('now', '-7 days')
        GROUP BY lm.name
    """).fetchall()

    # Plan distribution
    plans = db.execute("""
        SELECT plan_id, COUNT(*) as cnt, SUM(price_paid) as revenue
        FROM user_subscriptions WHERE status='paid'
        GROUP BY plan_id
    """).fetchall()

    return api_success({
        'users': {'total': total_users, 'active_today': active_today},
        'submissions': {'total': total_submissions},
        'revenue': {'total': round(total_revenue, 2)},
        'model_performance': [dict(r) for r in model_stats],
        'plan_distribution': [dict(r) for r in plans],
    })
