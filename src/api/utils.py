import inspect
import sqlite3
import uuid
import os
from datetime import datetime
from functools import wraps
from typing import Optional

import jwt
import bcrypt
from flask import g, request, jsonify

from src.config import Config, JWT_ALGORITHM, JWT_SECRET


SCHEMA_COLUMN_MIGRATIONS = {
    'submissions': {
        'grading_mode': 'TEXT',
        'agreement_rate': 'REAL',
        'score_spread': 'REAL',
        'valid_judges': 'INT DEFAULT 0',
        'failed_judges': 'INT DEFAULT 0',
        'aggregate_json': 'TEXT',
    },
    'llm_models': {
        'deleted_at': 'DATETIME',
        'credit_cost': 'REAL',
    },
    'users': {
        'grading_credits': 'REAL DEFAULT 1.0',
    },
    'token_usage_logs': {
        'sid': 'TEXT',
    },
    'exchange_codes': {
        'package_id': 'INTEGER',
    },
    'code_redemptions': {
        'package_id': 'INTEGER',
    },
}

NEW_TABLES = {
    'chat_messages': """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            uid         TEXT NOT NULL,
            sid         TEXT NOT NULL,
            role        TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            content     TEXT NOT NULL,
            created_at  DATETIME DEFAULT (datetime('now')),
            FOREIGN KEY (uid) REFERENCES users(uid),
            FOREIGN KEY (sid) REFERENCES submissions(sid)
        );
        CREATE INDEX IF NOT EXISTS idx_chat_msg_uid_sid ON chat_messages(uid, sid);
    """,
    'exchange_codes': """
        CREATE TABLE IF NOT EXISTS exchange_codes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            code        TEXT UNIQUE NOT NULL,
            credits     REAL NOT NULL DEFAULT 5.0,
            max_uses    INTEGER NOT NULL DEFAULT 1,
            used_count  INTEGER NOT NULL DEFAULT 0,
            created_by  TEXT,
            created_at  DATETIME DEFAULT (datetime('now')),
            expires_at  DATETIME,
            status      TEXT DEFAULT 'active'
        )
    """,
    'code_redemptions': """
        CREATE TABLE IF NOT EXISTS code_redemptions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            code            TEXT NOT NULL,
            uid             TEXT NOT NULL,
            credits_granted REAL NOT NULL,
            redeemed_at     DATETIME DEFAULT (datetime('now')),
            FOREIGN KEY (uid) REFERENCES users(uid)
        )
    """,
    'credit_transactions': """
        CREATE TABLE IF NOT EXISTS credit_transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            uid         TEXT NOT NULL,
            trans_type  TEXT NOT NULL,
            amount      REAL NOT NULL,
            balance     REAL NOT NULL,
            detail      TEXT DEFAULT '',
            created_at  DATETIME DEFAULT (datetime('now'))
        )
    """,
    'packages': """
        CREATE TABLE IF NOT EXISTS packages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            description     TEXT DEFAULT '',
            package_type    TEXT NOT NULL DEFAULT 'usage',
            credits         INTEGER NOT NULL DEFAULT 0,
            duration_days   INTEGER NOT NULL DEFAULT 0,
            price           REAL NOT NULL DEFAULT 0,
            badge_name      TEXT DEFAULT '',
            badge_color     TEXT DEFAULT '#0c8ee7',
            sort_order      INTEGER DEFAULT 0,
            is_active       INTEGER DEFAULT 1,
            created_at      DATETIME DEFAULT (datetime('now')),
            updated_at      DATETIME DEFAULT (datetime('now'))
        )
    """,
    'user_packages': """
        CREATE TABLE IF NOT EXISTS user_packages (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            uid                 TEXT NOT NULL,
            package_id          INTEGER NOT NULL,
            code                TEXT DEFAULT '',
            package_name        TEXT NOT NULL,
            package_type        TEXT NOT NULL,
            total_credits       INTEGER NOT NULL DEFAULT 0,
            remaining_credits   INTEGER NOT NULL DEFAULT 0,
            total_days          INTEGER NOT NULL DEFAULT 0,
            badge_name          TEXT DEFAULT '',
            badge_color         TEXT DEFAULT '',
            is_active           INTEGER DEFAULT 1,
            expires_at          DATETIME,
            redeemed_at         DATETIME DEFAULT (datetime('now')),
            created_at          DATETIME DEFAULT (datetime('now')),
            FOREIGN KEY (uid) REFERENCES users(uid),
            FOREIGN KEY (package_id) REFERENCES packages(id)
        );
        CREATE INDEX IF NOT EXISTS idx_user_pkg_uid ON user_packages(uid);
        CREATE INDEX IF NOT EXISTS idx_user_pkg_active ON user_packages(uid, is_active);
    """,
}


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(Config.DATABASE_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def ensure_schema_columns(db) -> None:
    """Apply additive SQLite column migrations without touching existing rows."""
    for table_name, columns in SCHEMA_COLUMN_MIGRATIONS.items():
        table_exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        if not table_exists:
            continue
        existing = {
            row[1] for row in db.execute(f'PRAGMA table_info("{table_name}")')
        }
        for column_name, definition in columns.items():
            if column_name in existing:
                continue
            db.execute(
                f'ALTER TABLE "{table_name}" '
                f'ADD COLUMN "{column_name}" {definition}'
            )
    _migrate_simulation_records_id(db)


def _migrate_simulation_records_id(db) -> None:
    """simulation_records.id must be TEXT: services insert UUIDs, not integers."""
    table = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'simulation_records'"
    ).fetchone()
    if not table:
        return
    columns = list(db.execute("PRAGMA table_info(simulation_records)"))
    id_col = next((col for col in columns if col[1] == 'id'), None)
    if not id_col:
        return
    col_type = str(id_col[2] or '').upper()
    if 'INT' not in col_type:
        return

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS simulation_records_v2 (
            id               TEXT PRIMARY KEY,
            uid              TEXT NOT NULL,
            pid              TEXT NOT NULL,
            started_at       DATETIME NOT NULL,
            submitted_at     DATETIME,
            time_spent       INT,
            total_score      REAL,
            question_scores  TEXT,
            rank_percentile  REAL,
            status           TEXT DEFAULT 'in_progress',
            FOREIGN KEY (uid) REFERENCES users(uid),
            FOREIGN KEY (pid) REFERENCES papers(pid)
        );
        INSERT INTO simulation_records_v2
            (id, uid, pid, started_at, submitted_at, time_spent, total_score,
             question_scores, rank_percentile, status)
        SELECT CAST(id AS TEXT), uid, pid, started_at, submitted_at, time_spent,
               total_score, question_scores, rank_percentile, status
        FROM simulation_records;
        DROP TABLE simulation_records;
        ALTER TABLE simulation_records_v2 RENAME TO simulation_records;
        CREATE INDEX IF NOT EXISTS idx_sim_uid ON simulation_records(uid);
        CREATE INDEX IF NOT EXISTS idx_sim_pid ON simulation_records(pid);
        """
    )


def init_db():
    """Initialize database with schema and seed data"""
    db_path = Config.DATABASE_PATH
    admin_username = (os.getenv('ADMIN_USERNAME') or 'admin').strip()
    admin_password = os.getenv('ADMIN_PASSWORD', '')
    is_production = (
        os.getenv('ENV') == 'production'
        or os.getenv('FLASK_ENV') == 'production'
    )
    if is_production and not admin_password:
        raise RuntimeError('生产环境必须设置 ADMIN_PASSWORD')
    if is_production and len(admin_password) < 16:
        raise RuntimeError('ADMIN_PASSWORD 至少 16 个字符')

    os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else '.', exist_ok=True)
    db = sqlite3.connect(db_path)

    # Use absolute path relative to project root (works in Docker and local)
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    schema_path = os.path.join(project_root, 'data', 'schema.sql')
    with open(schema_path, 'r', encoding='utf-8') as f:
        db.executescript(f.read())
    # Create new tables not in schema.sql
    for table_sql in NEW_TABLES.values():
        db.executescript(table_sql)
    ensure_schema_columns(db)
    # Initialize grading credits: 9999 for admins, 3 for new users, keep existing
    db.execute(
        "UPDATE users SET grading_credits = 9999 WHERE role IN ('admin', 'super_admin') AND (grading_credits IS NULL OR grading_credits < 9999)"
    )
    db.execute(
        "UPDATE users SET grading_credits = 1.0 WHERE role NOT IN ('admin', 'super_admin') AND grading_credits IS NULL"
    )
    db.commit()

    admin_exists = db.execute(
        "SELECT 1 FROM users WHERE role IN ('admin', 'super_admin') LIMIT 1"
    ).fetchone()
    if not admin_exists and admin_password:
        password_hash = bcrypt.hashpw(
            admin_password.encode('utf-8'), bcrypt.gensalt()
        ).decode('utf-8')
        db.execute(
            """INSERT OR IGNORE INTO users
               (uid, username, password_hash, nickname, role, status)
               VALUES (?, ?, ?, ?, 'super_admin', 'active')""",
            (
                'admin_' + generate_uuid(),
                admin_username,
                password_hash,
                '管理员',
            ),
        )
        db.commit()

    # Load seed data if tables are empty
    count = db.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    if count == 0:
        seed_files = [
            'data/seed_papers.sql',
            'data/seed_phrases.sql',
        ]
        for sf in seed_files:
            sf_path = os.path.join(project_root, sf)
            if os.path.exists(sf_path):
                with open(sf_path, 'r', encoding='utf-8') as f:
                    try:
                        db.executescript(f.read())
                    except Exception as e:
                        print(f"Warning: failed to load {sf}: {e}")
        db.commit()

    # Load topics seed if hot_topics is empty
    count = db.execute("SELECT COUNT(*) FROM hot_topics").fetchone()[0]
    if count == 0:
        topics_path = os.path.join(project_root, 'data', 'seed_topics.sql')
        if os.path.exists(topics_path):
            with open(topics_path, 'r', encoding='utf-8') as f:
                try:
                    db.executescript(f.read())
                except Exception as e:
                    print(f"Warning: failed to load seed_topics.sql: {e}")
            db.commit()

    # Load community seed if community_posts is empty
    count = db.execute("SELECT COUNT(*) FROM community_posts").fetchone()[0]
    if count == 0:
        community_path = os.path.join(project_root, 'data', 'seed_community.sql')
        if os.path.exists(community_path):
            with open(community_path, 'r', encoding='utf-8') as f:
                try:
                    db.executescript(f.read())
                except Exception as e:
                    print(f"Warning: failed to load seed_community.sql: {e}")
            db.commit()

    db.close()


def generate_uuid():
    return str(uuid.uuid4())


def generate_sid():
    return 'sid_' + generate_uuid()


def api_success(data=None, message="ok"):
    return jsonify({"data": data, "message": message}), 200


def api_error(message, code=400):
    return jsonify({"error": message, "code": code}), code


def clamp_per_page(per_page, max_val: int = 100) -> int:
    """限制每页数量，防止恶意请求过大值。兼容 query string 传入的 str。"""
    try:
        value = int(per_page)
    except (TypeError, ValueError):
        value = 20
    try:
        ceiling = int(max_val)
    except (TypeError, ValueError):
        ceiling = 100
    ceiling = max(1, ceiling)
    return max(1, min(value, ceiling))


def _extract_user_from_token():
    """从请求中提取用户信息，返回 (user_dict, error_response)"""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return None, None  # 没有 token，不算错误
    token = auth_header.replace('Bearer ', '')
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        jti = data.get('jti')
        if jti:
            blacklisted = get_db().execute(
                "SELECT 1 FROM token_blacklist WHERE jti = ?", (jti,)
            ).fetchone()
            if blacklisted:
                return None, api_error("Token已失效，请重新登录", 401)
        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE uid = ? AND status = 'active'",
            (data['sub'],)
        ).fetchone()
        if not user:
            return None, api_error("User not found", 401)
        return dict(user), None
    except jwt.ExpiredSignatureError:
        return None, api_error("Token expired", 401)
    except jwt.InvalidTokenError:
        return None, api_error("Invalid token", 401)


def _accepts_kwarg(func, name: str) -> bool:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return True
    if name in signature.parameters:
        return True
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _with_current_user(func, kwargs, user):
    if _accepts_kwarg(func, 'current_user'):
        kwargs['current_user'] = user
    else:
        kwargs.pop('current_user', None)
    return kwargs


def token_required(f):
    """强制要求登录的装饰器"""
    @wraps(f)
    def decorated(*args, **kwargs):
        user, err = _extract_user_from_token()
        if err:
            return err
        if not user:
            return api_error("Token required", 401)
        return f(*args, **_with_current_user(f, kwargs, user))
    return decorated


def optional_token(f):
    """可选登录装饰器：有 token 则解析用户，没有则 current_user=None"""
    @wraps(f)
    def decorated(*args, **kwargs):
        user, err = _extract_user_from_token()
        if err:
            return err
        return f(*args, **_with_current_user(f, kwargs, user))
    return decorated


ROLE_PERMISSIONS = {
    'super_admin': ['*'],
    'admin': [
        'users.view', 'users.edit', 'users.ban',
        'papers.view', 'papers.add', 'papers.edit', 'papers.delete',
        'phrases.view', 'phrases.approve',
        'submissions.view', 'submissions.review',
        'stats.view', 'logs.view'
    ],
    'reviewer': [
        'submissions.view', 'submissions.review',
        'phrases.view', 'phrases.approve'
    ],
    'operator': [
        'papers.view', 'papers.add', 'papers.edit', 'papers.delete',
        'phrases.view', 'phrases.add'
    ]
}


def has_permission(role: str, permission: str) -> bool:
    """Check if a role has a specific permission"""
    if not permission:
        return True
    perms = ROLE_PERMISSIONS.get(role, [])
    if '*' in perms:
        return True
    # Check wildcard match (e.g., 'papers.*' matches 'papers.view')
    resource = permission.split('.')[0] if '.' in permission else ''
    if f'{resource}.*' in perms:
        return True
    return permission in perms


def _resolve_admin_user():
    """Resolve admin identity from Bearer token first, then Flask session."""
    management_roles = ('super_admin', 'admin', 'reviewer', 'operator')
    user, err = _extract_user_from_token()
    if err:
        return None, err
    if user and user.get('role') in management_roles and user.get('status', 'active') == 'active':
        return user, None

    try:
        from flask import session
        session_user = session.get('admin_user')
    except Exception:
        session_user = None
    if isinstance(session_user, dict) and session_user.get('uid'):
        row = get_db().execute(
            "SELECT * FROM users WHERE uid = ? AND status = 'active'",
            (session_user.get('uid'),),
        ).fetchone()
        if row:
            resolved = dict(row)
            if resolved.get('role') in management_roles:
                return resolved, None
        # Fall back to session payload when DB row is incomplete but role is valid.
        if session_user.get('role') in management_roles:
            return {
                'uid': session_user.get('uid'),
                'username': session_user.get('username'),
                'nickname': session_user.get('nickname'),
                'role': session_user.get('role'),
                'status': 'active',
            }, None
    return None, None


def admin_required(permission=None):
    """Admin permission decorator (Bearer token or admin session cookie)."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user, err = _resolve_admin_user()
            if err:
                return err
            if not user:
                return api_error("Admin login required", 401)
            role = user.get('role', 'user')
            if role not in ('super_admin', 'admin', 'reviewer', 'operator'):
                return api_error("Admin access required", 403)
            if not has_permission(role, permission):
                return api_error("Permission denied", 403)
            return f(*args, **_with_current_user(f, kwargs, user))
        return decorated
    return decorator


def get_user_by_id(uid: str) -> Optional[dict]:
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
    return dict(user) if user else None


def get_user_by_username(username: str) -> Optional[dict]:
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(user) if user else None


