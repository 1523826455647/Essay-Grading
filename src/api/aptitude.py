# -*- coding: utf-8 -*-
"""行测套题统计 API（管理员私有，不对外展示）

用户（管理员）每做完一套行测卷，手工录入：套题名称、做题日期、
五个模块（常识判断/言语理解/数量关系/判断推理/资料分析）的正确数与
总题数、按自己口径填写的主观总分。本模块负责持久化并输出统计：

- POST /api/aptitude/tests          新增一条套题记录
- GET  /api/aptitude/tests          全部记录（按日期倒序）
- PUT  /api/aptitude/tests/<id>     修改
- DELETE /api/aptitude/tests/<id>   删除
- GET  /api/aptitude/summary        汇总统计（各模块累计正确率、总分趋势等）

所有接口仅管理员可用（admin_required）。
"""
from flask import Blueprint

from src.api.utils import api_success, api_error, admin_required, get_db

aptitude_bp = Blueprint("aptitude", __name__, url_prefix="/api/aptitude")

# 五个固定模块：字段前缀 -> 中文名（顺序即展示顺序）
MODULES = [
    ("common", "常识判断"),
    ("verbal", "言语理解"),
    ("quant", "数量关系"),
    ("judge", "判断推理"),
    ("material", "资料分析"),
]


def _modules_dict(data: dict) -> dict:
    """从请求 payload 中提取并校验五个模块的正确/总题数。"""
    out = {}
    for key, label in MODULES:
        try:
            total = int(float(data.get(f"{key}_total") or 0))
            correct = int(float(data.get(f"{key}_correct") or 0))
        except (TypeError, ValueError):
            raise ValueError(f"{label}的正确数/总题数必须是数字")
        if total < 0 or correct < 0:
            raise ValueError(f"{label}的题数不能为负数")
        if correct > total:
            raise ValueError(f"{label}的正确数不能大于总题数")
        out[f"{key}_total"] = total
        out[f"{key}_correct"] = correct
    return out


def _validate_payload(data: dict, require_name: bool = True) -> dict:
    """校验并规范化录入数据，返回可直接入库的字段字典。"""
    out = {}
    paper_name = str(data.get("paper_name") or "").strip()
    if require_name and not paper_name:
        raise ValueError("请填写套题名称")
    if paper_name:
        out["paper_name"] = paper_name[:120]

    test_date = str(data.get("test_date") or "").strip()
    if require_name and not test_date:
        raise ValueError("请选择做题日期")
    if test_date:
        try:
            from datetime import datetime as _dt
            _dt.strptime(test_date, "%Y-%m-%d")
        except ValueError:
            raise ValueError("日期格式应为 YYYY-MM-DD")
        out["test_date"] = test_date

    out.update(_modules_dict(data))

    if "total_score" in data or require_name:
        raw = data.get("total_score")
        if raw is None or str(raw).strip() == "":
            out["total_score"] = None
        else:
            try:
                out["total_score"] = float(raw)
            except (TypeError, ValueError):
                raise ValueError("总分必须是数字")

    if "score_max" in data:
        try:
            smax = float(data.get("score_max") or 100)
        except (TypeError, ValueError):
            raise ValueError("总分满分必须是数字")
        if smax <= 0:
            raise ValueError("总分满分必须大于 0")
        out["score_max"] = smax

    if "duration_minutes" in data:
        raw = data.get("duration_minutes")
        if raw is None or str(raw).strip() == "":
            out["duration_minutes"] = None
        else:
            try:
                out["duration_minutes"] = int(float(raw))
            except (TypeError, ValueError):
                raise ValueError("用时必须是数字（分钟）")

    if "notes" in data:
        out["notes"] = (str(data.get("notes") or "").strip() or None)

    return out


@aptitude_bp.route("/tests", methods=["POST"])
@admin_required("*")
def create_test(current_user):
    """新增一条行测套题记录。"""
    data = request_json()
    try:
        fields = _validate_payload(data, require_name=True)
    except ValueError as e:
        return api_error(str(e), 400)

    db = get_db()
    cols = ", ".join(["uid", *fields.keys()])
    marks = ", ".join(["?"] * (len(fields) + 1))
    db.execute(
        f"INSERT INTO aptitude_tests ({cols}) VALUES ({marks})",
        (current_user["uid"], *fields.values()),
    )
    db.commit()
    return api_success({"id": db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]},
                       message="已录入")


@aptitude_bp.route("/tests", methods=["GET"])
@admin_required("*")
def list_tests(current_user):
    """全部套题记录（按日期倒序）。"""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM aptitude_tests ORDER BY test_date DESC, id DESC"
    ).fetchall()
    return api_success({"tests": [dict(r) for r in rows]})


@aptitude_bp.route("/tests/<int:test_id>", methods=["PUT"])
@admin_required("*")
def update_test(current_user, test_id: int):
    """修改一条套题记录。"""
    db = get_db()
    if not db.execute("SELECT 1 FROM aptitude_tests WHERE id=?", (test_id,)).fetchone():
        return api_error("记录不存在", 404)
    data = request_json()
    try:
        fields = _validate_payload(data, require_name=False)
    except ValueError as e:
        return api_error(str(e), 400)
    if not fields:
        return api_error("没有需要修改的字段", 400)
    sets = ", ".join(f"{k} = ?" for k in fields)
    db.execute(
        f"UPDATE aptitude_tests SET {sets} WHERE id=?",
        (*fields.values(), test_id),
    )
    db.commit()
    return api_success(message="已保存")


@aptitude_bp.route("/tests/<int:test_id>", methods=["DELETE"])
@admin_required("*")
def delete_test(current_user, test_id: int):
    """删除一条套题记录。"""
    db = get_db()
    if not db.execute("SELECT 1 FROM aptitude_tests WHERE id=?", (test_id,)).fetchone():
        return api_error("记录不存在", 404)
    db.execute("DELETE FROM aptitude_tests WHERE id=?", (test_id,))
    db.commit()
    return api_success(message="已删除")


@aptitude_bp.route("/summary", methods=["GET"])
@admin_required("*")
def summary(current_user):
    """汇总统计：各模块累计正确率 + 总分概况 + 趋势。"""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM aptitude_tests ORDER BY test_date ASC, id ASC"
    ).fetchall()
    tests = [dict(r) for r in rows]

    modules = []
    for key, label in MODULES:
        correct = sum(t.get(f"{key}_correct") or 0 for t in tests)
        total = sum(t.get(f"{key}_total") or 0 for t in tests)
        modules.append({
            "key": key,
            "name": label,
            "correct": correct,
            "total": total,
            "rate": round(correct / total * 100, 1) if total else None,
        })

    scores = [t.get("total_score") for t in tests if t.get("total_score") is not None]
    trend = [
        {
            "date": t.get("test_date"),
            "paper_name": t.get("paper_name"),
            "total_score": t.get("total_score"),
            "score_max": t.get("score_max") or 100,
        }
        for t in tests if t.get("total_score") is not None
    ]

    return api_success({
        "test_count": len(tests),
        "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
        "best_score": max(scores) if scores else None,
        "latest_score": scores[-1] if scores else None,
        "modules": modules,
        "trend": trend,
    })


def request_json() -> dict:
    from flask import request
    return request.get_json(silent=True) or {}
