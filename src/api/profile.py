# -*- coding: utf-8 -*-
"""个人学习档案 API

提供前端 profile.html 所需的完整档案数据，并支持手动/自动刷新统计。

背景：前端早已读取 user_dimension_trends / daily_practice / user_weak_points
      三张表，但此前无任何写入逻辑，导致能力雷达、题型分布、薄弱项全部空白。
      现由 profile_stats_service 负责聚合写入，本模块负责对外输出。
"""
from flask import Blueprint

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
        return api_success(stats)
    except Exception as e:  # noqa: BLE001
        return api_error(f"获取档案失败: {e}")


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
