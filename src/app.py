import os
import sys

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, render_template, send_from_directory, abort, redirect, request

from src.config import Config
from src.api.utils import close_db, init_db
from src.api.auth import auth_bp
from src.api.papers import papers_bp
from src.api.submissions import submissions_bp
from src.api.admin import admin_bp
from src.api.ocr import ocr_bp
from src.api.custom_grade import custom_grade_bp
from src.api.codes import codes_bp
from src.api.subs import subs_bp
from src.api.drill import drill_bp
from src.api.diagnosis import diagnosis_bp
from src.api.phrases import phrases_bp
from src.api.topic import topic_bp
from src.api.grading_chat import grading_chat_bp
from src.api.community import community_bp
from src.api.tickets import tickets_bp
from src.api.profile import profile_bp


def create_app():
    app = Flask(__name__,
                template_folder='../templates',
                static_folder='../static')

    app.config.from_object(Config)
    app.teardown_appcontext(close_db)

    # Register blueprints -- grading only (practice features removed)
    app.register_blueprint(auth_bp)
    app.register_blueprint(papers_bp)
    app.register_blueprint(submissions_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(ocr_bp)
    app.register_blueprint(custom_grade_bp)
    app.register_blueprint(codes_bp)
    app.register_blueprint(subs_bp)
    app.register_blueprint(drill_bp)
    app.register_blueprint(diagnosis_bp)
    app.register_blueprint(phrases_bp)
    app.register_blueprint(topic_bp)
    app.register_blueprint(grading_chat_bp)
    app.register_blueprint(community_bp)
    app.register_blueprint(tickets_bp)
    app.register_blueprint(profile_bp)

    # Initialize database
    with app.app_context():
        init_db()

    # Page routes
    @app.route('/')
    def index():
        return render_template('index.html')

    @app.route('/login')
    def login():
        return render_template('login.html')

    @app.route('/register')
    def register():
        return render_template('register.html')

    @app.route('/papers')
    def papers():
        return render_template('papers.html')

    @app.route('/custom-grade')
    @app.route('/grade')
    def custom_grade_page():
        return render_template('custom_grade.html')

    @app.route('/exam/<pid>/<qid>')
    def exam(pid, qid):
        return render_template('exam.html', pid=pid, qid=qid)

    @app.route('/result/<sid>')
    def result(sid):
        return render_template('result.html', sid=sid)

    @app.route('/demo')
    def demo():
        return render_template('demo.html')

    @app.route("/health")
    @app.route("/api/health")
    def health():
        return {"status": "ok", "service": "slb"}, 200

    @app.route('/api/models')
    def public_models():
        from src.api.utils import api_success
        from src.services.model_registry import list_models

        fields = ("model_id", "name", "protocol", "model_name", "weight", "credit_cost")
        models = [
            {field: (float(model.get("credit_cost") or 0) if field == "credit_cost" else model[field])
             for field in fields}
            for model in list_models(public_only=True)
        ]
        return api_success({"models": models})

    @app.errorhandler(404)
    def page_not_found(e):
        return render_template('404.html'), 404

    @app.route('/favicon.ico')
    def favicon():
        from flask import Response
        svg = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            '<rect width="64" height="64" rx="12" fill="#1c1e54"/>'
            '<text x="32" y="44" font-family="serif" font-size="36" '
            'font-weight="700" text-anchor="middle" fill="#f5e9d4">\u7533</text>'
            '</svg>'
        )
        return Response(svg, mimetype='image/svg+xml')

    @app.errorhandler(500)
    def internal_error(e):
        return render_template('500.html'), 500

    @app.route('/profile')
    def profile():
        return render_template('profile.html')

    @app.route('/chat')
    def grading_chat_page():
        return render_template('chat.html')

    @app.route('/drill')
    def drill():
        return render_template('drill.html')

    @app.route('/diagnosis')
    def diagnosis():
        return render_template('diagnosis.html')

    @app.route('/phrases/study')
    def phrases_study():
        return render_template('phrases_study.html')

    @app.route('/phrases')
    def phrases_page():
        return render_template('phrases.html')

    @app.route('/phrases/generate')
    def phrases_generate():
        return render_template('phrases_generate.html')

    @app.route('/topics')
    def topics():
        return render_template('topics.html')

    @app.route('/community')
    def community():
        return render_template('community.html')

    @app.route('/tickets')
    def tickets():
        return render_template('tickets.html')

    @app.route('/topics/<int:topic_id>')
    def topic_detail(topic_id):
        import jwt
        from src.config import JWT_SECRET, JWT_ALGORITHM
        from src.services import topic_service
        uid = None
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            try:
                data = jwt.decode(auth[7:], JWT_SECRET, algorithms=[JWT_ALGORITHM])
                uid = data.get('sub')
            except Exception:
                pass
        topic = topic_service.get_topic_detail(topic_id, uid)
        if not topic:
            abort(404)
        return render_template('topic_detail.html', topic=topic)

    # Admin routes
    def _admin_guard():
        # token + session 双通道判定（与登录接口写入 session 的设计配套）。
        # 注意：不要删 session 兜底——页面导航不带 Authorization 头，
        # 删了它登录成功后跳 /admin 也会被 302 弹回，后台将无法进入。
        from src.api.utils import _resolve_admin_user
        try:
            user, _err = _resolve_admin_user()
        except Exception:
            return False
        return bool(user)

    def _admin_page(template_name: str):
        if not _admin_guard():
            return redirect('/admin/login')
        return render_template(template_name)

    @app.route('/admin')
    def admin_index():
        return _admin_page('admin/dashboard.html')

    @app.route('/admin/login')
    def admin_login():
        # 进入登录页即清除残留的管理员 session（打破死循环的关键）：
        # token 过期时 API 返回 401、前端跳登录页；若旧 session 仍有效，
        # guard 会判定"已登录"把人踢回 /admin 造成循环。此处清掉 session，
        # 保证登录页总是可达；重新登录后写入全新 session + token。
        from flask import session as _sess
        try:
            _sess.pop('admin_user', None)
        except Exception:
            pass
        if _admin_guard():
            return redirect('/admin')
        return render_template('admin/login.html')

    @app.route('/admin/users')
    def admin_users():
        return _admin_page('admin/users.html')

    @app.route('/admin/papers')
    def admin_papers():
        return _admin_page('admin/papers.html')

    @app.route('/admin/models')
    def admin_models():
        return _admin_page('admin/models.html')

    @app.route('/admin/feature-models')
    def admin_feature_models():
        return _admin_page('admin/feature_models.html')

    @app.route('/admin/reviews')
    def admin_reviews():
        return _admin_page('admin/reviews.html')

    @app.route('/admin/stats')
    def admin_stats():
        return _admin_page('admin/stats.html')

    @app.route('/admin/logs')
    def admin_logs():
        return _admin_page('admin/logs.html')

    @app.route('/admin/phrases')
    def admin_phrases():
        return _admin_page('admin/phrases.html')

    @app.route('/admin/settings')
    def admin_settings():
        return _admin_page('admin/settings.html')

    @app.route('/admin/usage')
    def admin_usage():
        return _admin_page('admin/usage.html')

    @app.route('/admin/codes')
    def admin_codes():
        return _admin_page('admin/codes.html')

    @app.route('/admin/packages')
    def admin_packages():
        return _admin_page('admin/packages.html')

    @app.route('/admin/tickets')
    def admin_tickets():
        return _admin_page('admin/tickets.html')

    @app.after_request
    def add_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-XSS-Protection'] = '1; mode=block'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        # CSP：限制脚本/样式来源（自身 + CDN 字体/图标/图表），禁外部对象嵌入
        if response.content_type and 'text/html' in response.content_type:
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' unpkg.com; "
                "style-src 'self' 'unsafe-inline' unpkg.com fonts.googleapis.com; "
                "font-src 'self' unpkg.com fonts.gstatic.com data:; "
                "img-src 'self' data: blob:; "
                "connect-src 'self'; "
                "object-src 'none'; frame-ancestors 'none'"
            )
        if request.is_secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        return response

    return app


app = create_app()

if __name__ == '__main__':
    # 生产环境（gunicorn 导入 app，不执行此块）强制关闭 debug；
    # 仅本地开发时由 FLASK_DEBUG/ENV 显式开启。
    is_prod = os.getenv('ENV') == 'production' or os.getenv('FLASK_ENV') == 'production'
    debug_enabled = (not is_prod) and (os.getenv('FLASK_DEBUG', '1') == '1')
    app.run(host='0.0.0.0', port=8790, debug=debug_enabled)
