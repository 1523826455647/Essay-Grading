import os
import sys
import logging
import secrets
import threading
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Validate critical secrets at import time.
# 安全语义：生产环境必须通过环境变量显式提供，否则拒绝启动。
_REQUIRED_SECRET_ENV = ('SECRET_KEY', 'JWT_SECRET')

def _is_production_env() -> bool:
    env = os.getenv('FLASK_ENV', '') or os.getenv('ENV', '')
    return env.lower() in ('production', 'prod')

for _env_key in _REQUIRED_SECRET_ENV:
    _val = (os.getenv(_env_key) or '').strip()
    if not _val:
        if _is_production_env():
            logger.error(f"FATAL: {_env_key} 未设置。生产环境无法以空密钥启动。")
            sys.exit(1)
        else:
            logger.warning(f"WARNING: {_env_key} 未设置。开发环境将使用生成的临时密钥。")


class Config:
    # 生产环境必须由 .env 显式提供；此处仅作开发/测试兜底，生成随机临时值，
    # 避免硬编码默认密钥。config 顶部已校验：生产缺失即拒绝启动。
    SECRET_KEY = os.getenv('SECRET_KEY', secrets.token_hex(32))
    JWT_SECRET = os.getenv('JWT_SECRET', secrets.token_hex(32))
    LLM_CREDENTIALS_KEY = os.getenv('LLM_CREDENTIALS_KEY', '')
    JWT_ALGORITHM = 'HS256'
    JWT_EXPIRE_DAYS = int(os.getenv('JWT_EXPIRE_DAYS', 7))

    DATABASE_PATH = os.getenv('DATABASE_PATH', 'data/slb.db')

    # LLM Config (defaults from env, overridden by DB settings)
    LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'deepseek')
    LLM_MODEL = os.getenv('LLM_MODEL', 'deepseek-chat')
    LLM_API_KEY = os.getenv('LLM_API_KEY', '')
    LLM_BASE_URL = os.getenv('LLM_BASE_URL', 'https://api.deepseek.com/v1')
    LLM_TEMPERATURE = float(os.getenv('LLM_TEMPERATURE', 0.3))
    LLM_MAX_TOKENS = int(os.getenv('LLM_MAX_TOKENS', 8000))
    LLM_TIMEOUT = int(os.getenv('LLM_TIMEOUT', 60))
    LLM_MAX_ATTEMPTS = int(os.getenv('LLM_MAX_ATTEMPTS', 2))
    LLM_RETRY_DELAY = float(os.getenv('LLM_RETRY_DELAY', 0.5))

    # Redis Cache
    REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
    REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
    REDIS_DB = int(os.getenv('REDIS_DB', 0))
    CACHE_TTL = int(os.getenv('CACHE_TTL', 86400))

    # Telegram Bot
    TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN', '')

    # Upload
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB
    UPLOAD_FOLDER = 'uploads'


config = Config()

# Module-level exports for convenience
SECRET_KEY = Config.SECRET_KEY
JWT_SECRET = Config.JWT_SECRET
JWT_ALGORITHM = Config.JWT_ALGORITHM
JWT_EXPIRE_DAYS = Config.JWT_EXPIRE_DAYS
DATABASE_PATH = Config.DATABASE_PATH
LLM_PROVIDER = Config.LLM_PROVIDER
LLM_MODEL = Config.LLM_MODEL
LLM_API_KEY = Config.LLM_API_KEY
LLM_BASE_URL = Config.LLM_BASE_URL
LLM_TEMPERATURE = Config.LLM_TEMPERATURE
LLM_MAX_TOKENS = Config.LLM_MAX_TOKENS
LLM_TIMEOUT = Config.LLM_TIMEOUT
LLM_MAX_ATTEMPTS = Config.LLM_MAX_ATTEMPTS
LLM_RETRY_DELAY = Config.LLM_RETRY_DELAY
REDIS_HOST = Config.REDIS_HOST
REDIS_PORT = Config.REDIS_PORT
REDIS_DB = Config.REDIS_DB
CACHE_TTL = Config.CACHE_TTL
TG_BOT_TOKEN = Config.TG_BOT_TOKEN


# ============================================================
# Dynamic LLM Config (reads from DB, falls back to env)
# ============================================================

_llm_cache = {}
_llm_cache_lock = threading.Lock()
_llm_cache_ttl = 30  # seconds


def get_llm_config() -> dict:
    """Get LLM configuration, preferring DB settings over env vars.

    Returns dict with keys: provider, model, api_key, base_url, temperature, max_tokens
    """
    import time

    with _llm_cache_lock:
        if _llm_cache and time.time() - _llm_cache.get('_ts', 0) < _llm_cache_ttl:
            return {k: v for k, v in _llm_cache.items() if k != '_ts'}

    # Try reading from DB
    db_config = {}
    try:
        import sqlite3
        db = sqlite3.connect(Config.DATABASE_PATH)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT key, value FROM settings WHERE key IN ('llm_provider','llm_model','llm_api_key','llm_base_url')"
        ).fetchall()
        db.close()
        for row in rows:
            import json
            try:
                val = json.loads(row['value'])
            except (json.JSONDecodeError, TypeError):
                val = row['value']
            db_config[row['key']] = val
    except Exception as e:
        logger.debug(f"Could not read LLM config from DB: {e}")

    # Build final config with DB values taking priority
    result = {
        'provider': db_config.get('llm_provider', Config.LLM_PROVIDER),
        'model': db_config.get('llm_model', Config.LLM_MODEL),
        'api_key': db_config.get('llm_api_key', Config.LLM_API_KEY),
        'base_url': db_config.get('llm_base_url', Config.LLM_BASE_URL),
        'temperature': Config.LLM_TEMPERATURE,
        'max_tokens': Config.LLM_MAX_TOKENS,
        'timeout': Config.LLM_TIMEOUT,
        'max_attempts': Config.LLM_MAX_ATTEMPTS,
        'retry_delay': Config.LLM_RETRY_DELAY,
    }

    with _llm_cache_lock:
        _llm_cache.clear()
        _llm_cache.update(result)
        _llm_cache['_ts'] = time.time()

    return result


def invalidate_llm_cache():
    """Force next get_llm_config() to re-read from DB."""
    with _llm_cache_lock:
        _llm_cache.clear()
