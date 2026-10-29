# -*- coding: utf-8 -*-
"""自定义批改安全修复的单元测试。

覆盖验收标准：
  - 同账号连交 12 次，第 7 次起 429（6 次/分）
  - 材料/答案/标题超长返回 400
  - 后台试卷列表默认不含自定义试卷；type=custom 可查
  - 后台批改记录列表与详情可用
  - 迁移脚本重复执行结果一致

测试用临时数据库与假模型（patch 掉真实 LLM 调用），不触达外部服务。
"""
import importlib.util
import os
import tempfile
import uuid

# —— 必须在导入 src.config 之前设置环境变量 ——
_TEST_DB = os.path.join(tempfile.mkdtemp(prefix="slb-test-"), "test.db")
os.environ["DATABASE_PATH"] = _TEST_DB
os.environ["SECRET_KEY"] = "test-secret-not-for-production"
os.environ["JWT_SECRET"] = "test-jwt-secret-not-for-production"
os.environ["MULTI_MODEL_ENABLED"] = "true"
os.environ["CUSTOM_GRADE_PER_MINUTE"] = "6"
os.environ["CUSTOM_GRADE_PER_DAY"] = "60"

from cryptography.fernet import Fernet  # noqa: E402

os.environ["LLM_CREDENTIALS_KEY"] = Fernet.generate_key().decode()

import pytest  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(scope="module")
def app_client():
    from src.app import create_app
    from src.api.utils import get_db
    from src.services.auth import create_token
    from src.services.model_registry import create_model
    from src.services.grader.types import JudgeResult

    app = create_app()
    app.config["TESTING"] = True

    uid = "user_test_" + uuid.uuid4().hex[:8]
    admin_uid = "admin_test_" + uuid.uuid4().hex[:8]
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO users (uid, username, password_hash, nickname, role, status, grading_credits) "
            "VALUES (?, ?, ?, ?, 'user', 'active', 100)",
            (uid, uid, "x", "测试用户"),
        )
        db.execute(
            "INSERT INTO users (uid, username, password_hash, nickname, role, status, grading_credits) "
            "VALUES (?, ?, ?, ?, 'super_admin', 'active', 999)",
            (admin_uid, admin_uid, "x", "测试管理员"),
        )
        db.commit()
        model_id = create_model({
            "name": "测试模型",
            "protocol": "openai",
            "base_url": "https://example.invalid/v1",
            "model_name": "test-model",
            "api_key": "test-key-not-real",
            "enabled": True,
            "public_visible": True,
        })

    client = app.test_client()

    # patch 真实 LLM 评分，返回固定 JudgeResult
    def fake_grade(config, question, user_answer, material):
        return JudgeResult(
            model_id=str(config.get("model_id") or model_id),
            score_rate=80.0,
            dimension_scores={"point_coverage": 80.0},
            hit_points=[],
            missing_points=[],
            ai_feedback="测试评语",
            improving_suggestions=[],
            raw_metadata={},
            latency_ms=1,
        )

    import src.api.custom_grade as cg
    cg.grade_with_model = fake_grade

    user_token = create_token(uid)
    admin_token = create_token(admin_uid)

    yield {
        "client": client,
        "uid": uid,
        "admin_uid": admin_uid,
        "model_id": model_id,
        "user_token": user_token,
        "admin_token": admin_token,
    }


def _body(model_id, idx=0):
    return {
        "material": ["材料一：测试材料内容。"],
        "stem": f"请概括测试要点（第{idx}次）并写一篇短文。",
        "user_answer": "这是测试答案内容，用于验证限流与校验逻辑。",
        "question_type": "guina",
        "score_max": 10,
        "title": f"测试自定义任务{idx}",
        "key_points": [{"point": "测试要点", "max_score": 10}],
        "mode": "fallback",
        "model_ids": [model_id],
    }


def _post(client, token, body):
    return client.post(
        "/api/custom-grade",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


def test_rate_limit_12_submissions(app_client):
    """连交 12 次：前 6 次放行，第 7 次起 429 且带 Retry-After。"""
    client = app_client["client"]
    token = app_client["user_token"]
    codes = []
    retry_headers = []
    for i in range(12):
        resp = _post(client, token, _body(app_client["model_id"], i))
        codes.append(resp.status_code)
        if resp.status_code == 429:
            retry_headers.append(resp.headers.get("Retry-After"))

    assert codes[:6] == [200] * 6, f"前 6 次应全部成功，实际 {codes[:6]}"
    assert all(c == 429 for c in codes[6:]), f"第 7 次起应为 429，实际 {codes[6:]}"
    assert all(h is not None and int(h) > 0 for h in retry_headers), "429 响应应带 Retry-After"


def test_length_validation(app_client):
    """超长材料/答案/标题返回 400（校验先于限流，不受配额影响）。"""
    client = app_client["client"]
    token = app_client["user_token"]
    model_id = app_client["model_id"]

    body = _body(model_id)
    body["material"] = ["x" * 20001]
    assert _post(client, token, body).status_code == 400

    body = _body(model_id)
    body["user_answer"] = "x" * 5001
    assert _post(client, token, body).status_code == 400

    body = _body(model_id)
    body["title"] = "x" * 101
    assert _post(client, token, body).status_code == 400


def test_admin_papers_isolation(app_client):
    """后台试卷列表默认不含自定义任务；type=custom 只返回自定义任务。"""
    client = app_client["client"]
    headers = {"Authorization": f"Bearer {app_client['admin_token']}"}

    official = client.get("/api/admin/papers?limit=100", headers=headers).get_json()["data"]
    assert all(p["status"] != "custom" for p in official["papers"]), "默认列表不应含自定义试卷"

    custom = client.get("/api/admin/papers?type=custom&limit=100", headers=headers).get_json()["data"]
    assert custom["total"] >= 1
    assert all(p["status"] == "custom" for p in custom["papers"]), "自定义视图应只含自定义试卷"


def test_admin_submissions_list_and_detail(app_client):
    """后台批改记录列表可按用户筛选，详情含答题/材料/判定/token 字段。"""
    client = app_client["client"]
    headers = {"Authorization": f"Bearer {app_client['admin_token']}"}

    listing = client.get(
        f"/api/admin/submissions?uid={app_client['uid']}&limit=10", headers=headers
    ).get_json()["data"]
    assert listing["total"] >= 1
    sid = listing["submissions"][0]["sid"]

    detail = client.get(f"/api/admin/submissions/{sid}", headers=headers).get_json()["data"]
    assert detail["submission"]["sid"] == sid
    assert "user_answer" in detail["submission"]
    assert isinstance(detail["material"], list)
    assert isinstance(detail["judgments"], list)
    assert isinstance(detail["tokens"], list)


def test_migration_idempotent():
    """迁移重复执行结果一致：第二次 0 行更新。"""
    import sqlite3

    spec = importlib.util.spec_from_file_location(
        "migrate_20261029",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "migrate_20261029.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    tmp = os.path.join(tempfile.mkdtemp(prefix="slb-mig-"), "m.db")
    db = sqlite3.connect(tmp)
    db.execute("CREATE TABLE papers (pid TEXT PRIMARY KEY, source TEXT, status TEXT)")
    db.execute("INSERT INTO papers VALUES ('custom_abc', '用户上传', 'custom')")
    db.execute("INSERT INTO papers VALUES ('custom_def', '用户上传', 'custom')")
    db.execute("INSERT INTO papers VALUES ('gwy_2026_xzzf', '官方真题PDF', 'published')")
    db.commit()
    db.close()

    first = mod.migrate(tmp)
    second = mod.migrate(tmp)

    assert first["status_updated"] == 0       # status 已是 custom
    assert first["source_updated"] == 2       # source 规范化 2 行
    assert second["status_updated"] == 0
    assert second["source_updated"] == 0      # 第二次无变化
    assert first["audit_table"] and second["audit_table"]

    db = sqlite3.connect(tmp)
    rows = dict(db.execute("SELECT pid, source FROM papers").fetchall())
    db.close()
    assert rows["custom_abc"] == "custom"
    assert rows["custom_def"] == "custom"
    assert rows["gwy_2026_xzzf"] == "官方真题PDF"
