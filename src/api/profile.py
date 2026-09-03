# -*- coding: utf-8 -*-
"""个人学习档案 API

提供前端 profile.html 所需的完整档案数据，并支持手动/自动刷新统计。

背景：前端早已读取 user_dimension_trends / daily_practice / user_weak_points
      三张表，但此前无任何写入逻辑，导致能力雷达、题型分布、薄弱项全部空白。
      现由 profile_stats_service 负责聚合写入，本模块负责对外输出。
"""
from flask import Blueprint, make_response

from src.api.utils import api_success, api_error, token_required, get_db
from src.services.profile_stats_service import (
    get_profile_stats,
    rebuild_user_stats,
    ABILITY_LABELS,
    THRESHOLDS,
)

profile_bp = Blueprint("profile", __name__, url_prefix="/api")


@profile_bp.route("/profile/stats", methods=["GET"])
@token_required
def get_stats(current_user):
    """获取个人学习档案（含空状态判定）。

    返回 has_enough_data，前端据此决定渲染图表还是引导页，
    从根本上避免"图表区域一片空白"。
    """
    uid = current_user["uid"]
    db = get_db()
    try:
        stats = get_profile_stats(uid, db=db)
        stats["ability_labels"] = ABILITY_LABELS
        resp = make_response(api_success(stats))
        # 统计数据实时变化，禁止浏览器缓存，避免前端一直显示旧值
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp
    except Exception as e:  # noqa: BLE001
        return api_error(f"获取档案失败: {e}")


@profile_bp.route("/leaderboard", methods=["GET"])
def get_leaderboard():
    """排行榜：按批改记录统计用户做题量与平均成绩。

    口径与个人档案一致——只统计批改记录（submissions），
    排名按平均成绩，展示做题量、今日/本周动态。至少 3 次批改才上榜。
    """
    from datetime import datetime, timedelta

    db = get_db()
    now = datetime.now()
    today = now.date().isoformat()
    week_ago = (now - timedelta(days=7)).date().isoformat()

    rows = db.execute(
        """SELECT u.uid, u.nickname, u.username,
                  COUNT(s.sid) AS total_count,
                  AVG(s.score) AS avg_score,
                  SUM(CASE WHEN substr(s.created_at, 1, 10) = ? THEN 1 ELSE 0 END) AS today_count,
                  SUM(CASE WHEN substr(s.created_at, 1, 10) >= ? THEN 1 ELSE 0 END) AS week_count
           FROM users u
           LEFT JOIN submissions s ON s.uid = u.uid AND s.score IS NOT NULL
           WHERE u.status = 'active'
           GROUP BY u.uid
           HAVING total_count >= 3
           ORDER BY avg_score DESC, total_count DESC
           LIMIT 20""",
        (today, week_ago),
    ).fetchall()

    board = []
    for i, r in enumerate(rows, 1):
        board.append({
            "rank": i,
            "uid": r["uid"],
            "nickname": (r["nickname"] or r["username"] or "匿名用户").strip(),
            "total_count": r["total_count"],
            "avg_score": round(float(r["avg_score"]), 1) if r["avg_score"] is not None else 0.0,
            "today_count": r["today_count"] or 0,
            "week_count": r["week_count"] or 0,
        })
    resp = make_response(api_success({"board": board}))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@profile_bp.route("/profile/refresh", methods=["POST"])
@token_required
def refresh_stats(current_user):
    """强制重建当前用户的档案统计（练习完成后调用）。"""
    uid = current_user["uid"]
    try:
        result = rebuild_user_stats(uid)
        return api_success({
            "total_practice": result["total_practice"],
            "streak_days": result["streak_days"],
            "abilities": result["abilities"],
            "weak_points_count": len(result["weak_points"]),
        })
    except Exception as e:  # noqa: BLE001
        return api_error(f"刷新档案失败: {e}")


@profile_bp.route("/profile/thresholds", methods=["GET"])
def get_thresholds():
    """返回各模块的空状态阈值，供前端统一判定。"""
    return api_success({"thresholds": THRESHOLDS})
