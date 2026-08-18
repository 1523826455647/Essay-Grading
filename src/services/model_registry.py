"""Encrypted model registry used by the admin API and grading runners."""

import os
import uuid

from cryptography.fernet import Fernet, InvalidToken

from src.api.utils import get_db

SUPPORTED_PROTOCOLS = {"openai", "anthropic"}


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


MAX_MODELS_PER_SUBMISSION = _bounded_env_int(
    "MAX_MODELS_PER_SUBMISSION", 4, 1, 4
)
DEFAULT_MODEL_TIMEOUT_SECONDS = 180
MAX_MODEL_TIMEOUT_SECONDS = 300
MODEL_FIELDS = {
    "name",
    "protocol",
    "base_url",
    "model_name",
    "weight",
    "priority",
    "enabled",
    "public_visible",
    "timeout_seconds",
    "max_tokens",
    "credit_cost",
    "input_price_per_mtok",
    "output_price_per_mtok",
}


def _is_production() -> bool:
    return os.getenv("ENV") == "production" or os.getenv("FLASK_ENV") == "production"


def validate_credentials_key() -> str:
    value = (os.getenv("LLM_CREDENTIALS_KEY") or "").strip()
    if not value:
        if _is_production():
            raise RuntimeError("生产环境必须设置 LLM_CREDENTIALS_KEY")
        raise RuntimeError("LLM_CREDENTIALS_KEY 未配置")
    try:
        Fernet(value.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise RuntimeError("LLM_CREDENTIALS_KEY 不是有效的 Fernet 密钥") from None
    return value


def _fernet() -> Fernet:
    return Fernet(validate_credentials_key().encode("ascii"))


def encrypt_api_key(api_key: str) -> str:
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("api_key 不能为空")
    return _fernet().encrypt(api_key.strip().encode("utf-8")).decode("ascii")


def decrypt_api_key(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError, UnicodeEncodeError):
        raise RuntimeError("模型凭据解密失败，请检查 LLM_CREDENTIALS_KEY") from None


def _validate_payload(payload: dict, require_key: bool = True) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("模型配置必须是对象")
    normalized = dict(payload)
    for field in ("name", "base_url", "model_name"):
        if not str(normalized.get(field, "")).strip():
            raise ValueError(f"{field} 不能为空")
    protocol = str(normalized.get("protocol", "")).strip().lower()
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ValueError("protocol 必须为 openai 或 anthropic")
    normalized["protocol"] = protocol
    if require_key and not str(normalized.get("api_key", "")).strip():
        raise ValueError("api_key 不能为空")
    if "weight" in normalized and float(normalized["weight"]) <= 0:
        raise ValueError("weight 必须大于 0")
    if "priority" in normalized and int(normalized["priority"]) < 0:
        raise ValueError("priority 不能为负数")
    if "timeout_seconds" in normalized and not 5 <= int(normalized["timeout_seconds"]) <= MAX_MODEL_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be between 5 and {MAX_MODEL_TIMEOUT_SECONDS}")
    if "max_tokens" in normalized:
        mt = int(normalized["max_tokens"])
        if mt != 0 and not 128 <= mt <= 200000:
            raise ValueError("max_tokens 必须为 0（不限制）或在 128 到 200000 之间")
    if "credit_cost" in normalized and float(normalized["credit_cost"]) < 0:
        raise ValueError("credit_cost 不能为负数")
    for pf in ("input_price_per_mtok", "output_price_per_mtok"):
        if pf in normalized:
            try:
                normalized[pf] = float(normalized[pf])
            except (TypeError, ValueError):
                raise ValueError(f"{pf} 必须是数字")
            if normalized[pf] < 0:
                raise ValueError(f"{pf} 不能为负数")
    return normalized


def _row_to_model(row, include_secret: bool = False) -> dict:
    model = dict(row)
    model["enabled"] = bool(model["enabled"])
    model["public_visible"] = bool(model["public_visible"])
    model["has_api_key"] = bool(model.pop("api_key_ciphertext", ""))
    if include_secret:
        model["api_key"] = decrypt_api_key(row["api_key_ciphertext"])
    return model


def create_model(payload: dict, commit: bool = True) -> str:
    data = _validate_payload(payload, require_key=True)
    model_id = data.get("model_id") or "model_" + uuid.uuid4().hex
    db = get_db()
    db.execute(
        """INSERT INTO llm_models
           (model_id, name, protocol, base_url, model_name, api_key_ciphertext,
            weight, priority, enabled, public_visible, timeout_seconds, max_tokens, credit_cost,
            input_price_per_mtok, output_price_per_mtok)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            model_id,
            str(data["name"]).strip(),
            data["protocol"],
            str(data["base_url"]).strip().rstrip("/"),
            str(data["model_name"]).strip(),
            encrypt_api_key(data["api_key"]),
            float(data.get("weight", 1.0)),
            int(data.get("priority", 100)),
            int(bool(data.get("enabled", True))),
            int(bool(data.get("public_visible", False))),
            int(data.get("timeout_seconds", DEFAULT_MODEL_TIMEOUT_SECONDS)),
            int(data.get("max_tokens", 8000)),
            float(data.get("credit_cost", 1.0)),
            float(data.get("input_price_per_mtok", 0.0)),
            float(data.get("output_price_per_mtok", 0.0)),
        ),
    )
    if commit:
        db.commit()
    return model_id


def get_model(
    model_id: str,
    include_secret: bool = False,
    include_deleted: bool = False,
) -> dict | None:
    query = "SELECT * FROM llm_models WHERE model_id = ?"
    if not include_deleted:
        query += " AND deleted_at IS NULL"
    row = get_db().execute(query, (model_id,)).fetchone()
    return _row_to_model(row, include_secret=include_secret) if row else None


def list_models(public_only: bool = False) -> list[dict]:
    query = "SELECT * FROM llm_models WHERE deleted_at IS NULL"
    params = []
    if public_only:
        query += " AND enabled = 1 AND public_visible = 1"
    query += " ORDER BY priority ASC, name ASC"
    rows = get_db().execute(query, params).fetchall()
    return [_row_to_model(row) for row in rows]


def update_model(model_id: str, payload: dict, commit: bool = True) -> None:
    existing = get_model(model_id)
    if not existing:
        raise ValueError("模型不存在")
    data = dict(payload)
    if "api_key" in data and not str(data["api_key"]).strip():
        data.pop("api_key")
    merged = dict(existing)
    merged.update(data)
    if "api_key" not in data:
        merged["api_key"] = decrypt_api_key(
            get_db().execute(
                """SELECT api_key_ciphertext FROM llm_models
                   WHERE model_id = ? AND deleted_at IS NULL""",
                (model_id,),
            ).fetchone()[0]
        )
    normalized = _validate_payload(merged, require_key=True)
    assignments = []
    values = []
    for field in MODEL_FIELDS:
        if field in normalized:
            assignments.append(f"{field} = ?")
            values.append(normalized[field])
    if "api_key" in data:
        assignments.append("api_key_ciphertext = ?")
        values.append(encrypt_api_key(normalized["api_key"]))
    assignments.append("updated_at = datetime('now')")
    values.append(model_id)
    get_db().execute(
        f"""UPDATE llm_models SET {', '.join(assignments)}
            WHERE model_id = ? AND deleted_at IS NULL""",
        values,
    )
    if commit:
        get_db().commit()


def update_test_status(
    model_id: str,
    status: str,
    error_category: str | None,
    latency_ms: int | None,
    commit: bool = True,
) -> None:
    if status not in {"success", "failure"}:
        raise ValueError("status must be success or failure")
    db = get_db()
    cursor = db.execute(
        """UPDATE llm_models
           SET last_test_status = ?, last_test_error = ?, last_latency_ms = ?,
               updated_at = datetime('now')
           WHERE model_id = ? AND deleted_at IS NULL""",
        (status, error_category, latency_ms, model_id),
    )
    if commit:
        db.commit()
    if cursor.rowcount == 0:
        raise ValueError("model not found")


def delete_model(model_id: str, commit: bool = True) -> str:
    db = get_db()
    referenced = db.execute(
        "SELECT 1 FROM submission_judgments WHERE model_id = ? LIMIT 1",
        (model_id,),
    ).fetchone()
    if referenced:
        cursor = db.execute(
            """UPDATE llm_models
               SET enabled = 0, public_visible = 0,
                   deleted_at = datetime('now'), updated_at = datetime('now')
               WHERE model_id = ? AND deleted_at IS NULL""",
            (model_id,),
        )
        disposition = "archived"
    else:
        cursor = db.execute(
            "DELETE FROM llm_models WHERE model_id = ? AND deleted_at IS NULL",
            (model_id,),
        )
        disposition = "deleted"
    if commit:
        db.commit()
    if cursor.rowcount == 0:
        raise ValueError("model not found")
    return disposition
