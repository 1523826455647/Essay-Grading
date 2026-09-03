# -*- coding: utf-8 -*-
"""个人学习档案 · 统计聚合服务

背景（本次修复的核心问题）：
    前端 profile.html 早已从以下三张表读取图表数据，但这三张表**从未被写入**：
        - user_dimension_trends  -> 能力雷达（无任何写入代码）
        - user_weak_points       -> 薄弱项（无任何写入代码）
        - daily_practice         -> 练习趋势（仅"每日一练"接口写，题型训练 71 条记录没进来）
    结果：能力雷达、题型分布、薄弱项全部显示空白。

本服务做的事：
    1. 从真实数据源（submissions / question_type_drills / diagnostic_reports）聚合
    2. 写入上述三张统计表 + daily_practice_streaks（连续学习天数）
    3. 提供 get_profile_stats() 给前端返回一份完整、可直接渲染的档案

归一化依据：
    src/services/grader/dimensions.py 中 QUESTION_TYPE_DIMENSIONS 定义了各题型的维度权重
    （权重和为 1.0，满分 100），因此：
        维度得分率 = 维度实际得分 / (100 × 该维度权重)

幂等性：所有写入均为先删后插 / INSERT OR REPLACE，可安全重复执行。
"""

from __future__ import annotations

import ast
import json
from collections import defaultdict
from datetime import datetime, timedelta

# ============================================================
# 分题型维度权重（与 src/services/grader/dimensions.py 保持一致）
# ============================================================
QUESTION_TYPE_DIMENSIONS = {
    "guina":   {"point_coverage": 0.70, "conciseness": 0.15, "accuracy": 0.10, "format": 0.05},
    "zonghe":  {"logic_chain": 0.30, "point_coverage": 0.30, "depth": 0.20,
                "language": 0.10, "format": 0.10},
    "duice":   {"problem_identification": 0.20, "targeting": 0.25, "feasibility": 0.25,
                "specificity": 0.20, "format": 0.10},
    "zhixing": {"format_correctness": 0.20, "purpose_achievement": 0.25,
                "content_completeness": 0.30, "language_appropriateness": 0.15,
                "word_count": 0.10},
    "zuowen":  {"thesis_accuracy": 0.25, "argument_richness": 0.25, "structure": 0.20,
                "language": 0.20, "innovation": 0.10},
}

# ============================================================
# 五维能力模型：能力维度 -> [(题型, 该题型下的维度键), ...]
# 说明：把各题型的具体评分维度映射到统一的五维能力，解决"不同题型维度键名打架"
#       导致雷达图无法聚合的问题。
# ============================================================
ABILITY_MAP = {
    "reading": [      # 阅读理解
        ("guina", "accuracy"), ("guina", "point_coverage"),
        ("zhixing", "content_completeness"),
    ],
    "summarize": [    # 归纳概括
        ("guina", "point_coverage"), ("guina", "conciseness"),
    ],
    "analyze": [      # 综合分析
        ("zonghe", "logic_chain"), ("zonghe", "depth"), ("zonghe", "point_coverage"),
        ("zuowen", "argument_richness"), ("zuowen", "innovation"),
    ],
    "solve": [        # 提出和解决问题
        ("duice", "problem_identification"), ("duice", "targeting"),
        ("duice", "feasibility"), ("duice", "specificity"),
        ("zhixing", "purpose_achievement"),
    ],
    "express": [      # 文字表达
        ("guina", "format"), ("zonghe", "language"), ("zonghe", "format"),
        ("duice", "format"), ("zhixing", "format_correctness"),
        ("zhixing", "language_appropriateness"),
        ("zuowen", "language"), ("zuowen", "structure"), ("zuowen", "thesis_accuracy"),
    ],
}

ABILITY_LABELS = {
    "reading": "阅读理解", "summarize": "归纳概括", "analyze": "综合分析",
    "solve": "提出对策", "express": "文字表达",
}

TYPE_LABELS = {
    "guina": "归纳概括", "zonghe": "综合分析", "duice": "提出对策",
    "zhixing": "贯彻执行", "zuowen": "申发论述",
}

# 空状态阈值（与前端统一口径）
THRESHOLDS = {
    "radar": 5,        # 能力雷达：至少 5 次练习
    "type_matrix": 10, # 题型矩阵：至少 10 次
    "trend": 3,        # 趋势图：至少 3 天记录
    "weakness": 15,    # 薄弱项诊断：至少 15 次
}


# ============================================================
# 工具函数
# ============================================================
def parse_dims(raw) -> dict:
    """解析 dimension_scores 字段。

    兼容三种情况：
      1) JSON 字符串   '{"a": 1}'
      2) Python str(dict)  "{'a': 1}"   （旧代码用 str() 存的）
      3) 已是 dict
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        return {k: float(v) for k, v in raw.items() if _is_num(v)}
    if not isinstance(raw, str):
        return {}
    s = raw.strip()
    if not s or s in ("{}", "[]", "null", "None"):
        return {}
    for loader in (json.loads, ast.literal_eval):
        try:
            d = loader(s)
            if isinstance(d, dict):
                return {k: float(v) for k, v in d.items() if _is_num(v)}
        except Exception:
            continue
    return {}


def _is_num(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _is_valid(dims: dict, score) -> bool:
    """判定一条记录是否为有效的已评分数据。

    问题背景：库中存在大量"未评分"脏数据——dimension_scores 各维度全为 0，
    或 score 为空。若直接参与计算，会把用户能力值拉到 0，导致雷达图显示异常。

    判定规则：
      - 维度字典非空且各维度全为 0  -> 未评分，无效
      - 维度为空且 score 为空或 0   -> 无效
      - 其余（有有效维度分，或有正的 score）-> 有效
    """
    if dims:
        # 有维度数据：全为 0 视为未评分
        return not all(float(v or 0) == 0 for v in dims.values())
    # 无维度数据：看 score
    if score is None:
        return False
    try:
        return float(score) > 0
    except (TypeError, ValueError):
        return False


def _dims_to_rates(qtype: str, dims: dict) -> dict:
    """把一条练习记录的维度分统一转为 0~1 得分率。

    系统存在两种存储口径：
      - 绝对分：各维度满分为 100×题型权重（如归纳题踩点命中满 70），
        一条记录各维度之和 ≤ 100（权重和恰好为 1）；
      - 百分比：新批改链路统一存 0-100 百分比，各维度之和可高达 500。
    按「维度和是否 > 110」自动识别口径：绝对分按 100×权重 折算，
    百分比直接 /100。避免旧绝对分被当成百分比（能力雷达被拉低）。
    """
    if not dims or not qtype:
        return {}
    weights = QUESTION_TYPE_DIMENSIONS.get(qtype)
    if not weights:
        return {}
    numeric = {k: float(v) for k, v in dims.items() if _is_num(v)}
    if not numeric:
        return {}
    is_percent = sum(numeric.values()) > 110.0
    rates = {}
    for k, v in numeric.items():
        w = weights.get(k)
        if not w:
            continue
        if is_percent:
            rates[k] = max(0.0, min(1.0, v / 100.0))
        else:
            rates[k] = max(0.0, min(1.0, v / (100.0 * w)))
    return rates


# ============================================================
# 采集：合并 submissions + question_type_drills
# ============================================================
def collect_practices(db, uid: str) -> list[dict]:
    """采集用户全部练习记录，统一结构。

    返回元素: {source, ref_id, qtype, score, dims, created_at}
    注意：question_type_drills 是题型训练主战场，此前完全没进统计，
          这是"练习趋势空白"的直接原因。
    """
    out: list[dict] = []

    # 1) 题型训练
    try:
        rows = db.execute(
            "SELECT id, question_type, score, dimension_scores, created_at "
            "FROM question_type_drills WHERE uid=?", (uid,)
        ).fetchall()
        for r in rows:
            d = dict(r) if not isinstance(r, dict) else r
            dims = parse_dims(d.get("dimension_scores"))
            out.append({
                "source": "drill",
                "ref_id": d.get("id"),
                "qtype": (d.get("question_type") or "").strip(),
                "score": d.get("score"),
                "dims": dims,
                "created_at": d.get("created_at") or "",
                "valid": _is_valid(dims, d.get("score")),
            })
    except Exception as e:
        print(f"[聚合] 读取 question_type_drills 失败: {e}")

    # 2) 提交批改（submissions）
    try:
        rows = db.execute(
            "SELECT sid, qid, score, dimension_scores, created_at, missing_points "
            "FROM submissions WHERE uid=?", (uid,)
        ).fetchall()
        for r in rows:
            d = dict(r) if not isinstance(r, dict) else r
            dims = parse_dims(d.get("dimension_scores"))
            out.append({
                "source": "submission",
                "ref_id": d.get("sid"),
                "qtype": "",   # submissions 未直接记录题型，维度键反推
                "score": d.get("score"),
                "dims": dims,
                "created_at": d.get("created_at") or "",
                "missing_points": d.get("missing_points"),
                "valid": _is_valid(dims, d.get("score")),
            })
    except Exception as e:
        print(f"[聚合] 读取 submissions 失败: {e}")

    # 3) 诊断报告（含五题型分数，能力雷达的重要补充源）
    try:
        rows = db.execute(
            "SELECT id, score_guina, score_zonghe, score_duice, score_zhixing, "
            "score_zuowen, overall_score, created_at FROM diagnostic_reports "
            "WHERE uid=?", (uid,)
        ).fetchall()
        for r in rows:
            d = dict(r) if not isinstance(r, dict) else r
            for qt in ("guina", "zonghe", "duice", "zhixing", "zuowen"):
                sc = d.get(f"score_{qt}")
                if sc is None:
                    continue
                try:
                    sc = float(sc)
                except (TypeError, ValueError):
                    continue
                if sc <= 0:      # 0 分视为该题型未纳入诊断，跳过
                    continue
                out.append({
                    "source": "diagnosis",
                    "ref_id": d.get("id"),
                    "qtype": qt,
                    "score": sc,
                    "dims": {},
                    "created_at": d.get("created_at") or "",
                    "valid": True,
                })
    except Exception as e:
        print(f"[聚合] 读取 diagnostic_reports 失败: {e}")

    # 按时间排序
    out.sort(key=lambda x: x.get("created_at") or "")
    return out


# ============================================================
# 计算：五维能力
# ============================================================
def calc_abilities(practices: list[dict]) -> dict:
    """计算五维能力得分（0~100）。

    策略：
      - 优先用维度归一化得分率（精确反映各细分能力）
      - 若某能力无维度数据，退化为用该题型的总分
    """
    bucket: dict[str, list[float]] = defaultdict(list)

    for p in practices:
        # 跳过未评分的脏数据（全 0 维度 / 空 score）
        if p.get("valid") is False:
            continue

        qt = p.get("qtype") or ""
        dims = p.get("dims") or {}

        # 传入的 qtype 为空时，用维度键反推题型（submissions 场景）
        if not qt and dims:
            qt = _guess_type(dims)
        if not qt:
            continue

        # 整条记录的维度统一折算（自动识别绝对分/百分比口径）
        rates = _dims_to_rates(qt, dims)
        score_rate = None
        if p.get("score") is not None:
            try:
                score_rate = max(0.0, min(1.0, float(p["score"]) / 100.0))
            except (TypeError, ValueError):
                score_rate = None

        for ability, pairs in ABILITY_MAP.items():
            bucket_vals = []
            for t, key in pairs:
                if t != qt:
                    continue
                if key in rates:
                    bucket_vals.append(rates[key])
            if bucket_vals:
                bucket[ability].extend(bucket_vals)
            elif score_rate is not None:
                # 退化：该题型下无细分维度数据，用总分（0~100 即得分率）
                bucket[ability].append(score_rate)

    result = {}
    for ability in ABILITY_MAP:
        vals = bucket.get(ability, [])
        result[ability] = round(sum(vals) / len(vals) * 100, 1) if vals else 0.0
    return result


def _guess_type(dims: dict) -> str:
    """用维度键反推题型（submissions 未记录题型时用）。"""
    keys = set(dims.keys())
    best, best_hit = "", 0
    for qt, weight_map in QUESTION_TYPE_DIMENSIONS.items():
        hit = len(keys & set(weight_map.keys()))
        if hit > best_hit:
            best, best_hit = qt, hit
    return best


# ============================================================
# 同步：写回统计表
# ============================================================
def sync_dimension_trends(db, uid: str, abilities: dict) -> None:
    """写入 user_dimension_trends（能力雷达数据源）。此前全项目零写入。"""
    try:
        db.execute("DELETE FROM user_dimension_trends WHERE uid=?", (uid,))
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for dim, score in abilities.items():
            db.execute(
                "INSERT INTO user_dimension_trends (uid, dim_name, avg_score, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (uid, dim, score, now),
            )
    except Exception as e:
        print(f"[聚合] 写入 user_dimension_trends 失败: {e}")


def sync_daily_practice(db, uid: str, practices: list[dict]) -> None:
    """写入 daily_practice（练习趋势数据源）。

    关键修复：把题型训练（question_type_drills）的记录也纳入，
    此前只统计"每日一练"，导致练了很多题但趋势图空白。
    """
    try:
        # 只清理本服务写入的记录（source 标记在 qid 前缀），
        # 避免误删"每日一练"接口写入的数据
        db.execute(
            "DELETE FROM daily_practice WHERE uid=? AND "
            "(qid LIKE 'drill:%' OR qid LIKE 'diag:%')", (uid,)
        )
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for p in practices:
            if p["source"] == "submission":
                continue  # submissions 由原接口负责，不重复写
            created = (p.get("created_at") or "")[:10]
            if not created:
                continue
            qid = f"{p['source']}:{p.get('ref_id')}"
            db.execute(
                "INSERT OR REPLACE INTO daily_practice "
                "(uid, practice_date, pid, qid, score, dimension_scores, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uid, created, "", qid, p.get("score"),
                 json.dumps(p.get("dims") or {}, ensure_ascii=False), now),
            )
    except Exception as e:
        print(f"[聚合] 写入 daily_practice 失败: {e}")


def sync_streaks(db, uid: str) -> int:
    """计算并写入连续学习天数。返回当前连续天数。"""
    try:
        rows = db.execute(
            "SELECT DISTINCT practice_date FROM daily_practice WHERE uid=? "
            "ORDER BY practice_date DESC", (uid,)
        ).fetchall()
        dates = [r[0] if not isinstance(r, dict) else r["practice_date"] for r in rows]
        dates = [d for d in dates if d]
        if not dates:
            return 0

        streak = 1
        today = datetime.now().strftime("%Y-%m-%d")
        # 若最近一次不是今天或昨天，连续中断
        newest = dates[0]
        if newest not in (today, (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")):
            streak = 0
        else:
            for i in range(len(dates) - 1):
                cur = datetime.strptime(dates[i], "%Y-%m-%d")
                nxt = datetime.strptime(dates[i + 1], "%Y-%m-%d")
                if (cur - nxt).days == 1:
                    streak += 1
                else:
                    break

        db.execute(
            "INSERT OR REPLACE INTO daily_practice_streaks (uid, streak, last_practice) "
            "VALUES (?, ?, ?)", (uid, streak, dates[0]),
        )
        return streak
    except Exception as e:
        print(f"[聚合] 写入 daily_practice_streaks 失败: {e}")
        return 0


def sync_weak_points(db, uid: str) -> list[dict]:
    """从 submissions.missing_points 提取薄弱采分点，写入 user_weak_points。

    missing_points 形如: [{"point": "...", "max_score": 15}, ...]
    """
    try:
        rows = db.execute(
            "SELECT missing_points FROM submissions WHERE uid=? AND "
            "missing_points IS NOT NULL AND missing_points != ''", (uid,)
        ).fetchall()

        counter: dict[str, int] = {}
        for r in rows:
            raw = r[0] if not isinstance(r, dict) else r["missing_points"]
            if not raw:
                continue
            try:
                items = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                continue
            if not isinstance(items, list):
                continue
            for it in items:
                if isinstance(it, dict):
                    point = (it.get("point") or "").strip()
                else:
                    point = str(it).strip()
                if point and len(point) < 200:
                    counter[point] = counter.get(point, 0) + 1

        db.execute("DELETE FROM user_weak_points WHERE uid=?", (uid,))
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for point, cnt in sorted(counter.items(), key=lambda x: -x[1])[:30]:
            db.execute(
                "INSERT INTO user_weak_points (uid, missing_point, cnt, updated_at) "
                "VALUES (?, ?, ?, ?)", (uid, point, cnt, now),
            )

        return [{"point": p, "count": c} for p, c in
                sorted(counter.items(), key=lambda x: -x[1])[:30]]
    except Exception as e:
        print(f"[聚合] 写入 user_weak_points 失败: {e}")
        return []


# ============================================================
# 主入口
# ============================================================
def rebuild_user_stats(uid: str, db=None) -> dict:
    """重建单个用户的全部统计。可重复执行（幂等）。"""
    own = db is None
    if own:
        from src.api.utils import get_db
        db = get_db()
    try:
        practices = collect_practices(db, uid)
        abilities = calc_abilities(practices)
        sync_dimension_trends(db, uid, abilities)
        sync_daily_practice(db, uid, practices)
        streak = sync_streaks(db, uid)
        weak = sync_weak_points(db, uid)

        # 题型分布
        type_dist: dict[str, dict] = {}
        for p in practices:
            qt = p.get("qtype") or (_guess_type(p["dims"]) if p.get("dims") else "")
            if not qt:
                continue
            acc = type_dist.setdefault(qt, {"count": 0, "total": 0.0, "n": 0})
            acc["count"] += 1
            if p.get("score") is not None:
                acc["total"] += float(p["score"])
                acc["n"] += 1

        if own:
            db.commit()

        return {
            "uid": uid,
            "total_practice": len(practices),
            "abilities": abilities,
            "streak_days": streak,
            "weak_points": weak,
            "type_distribution": {
                k: {"count": v["count"],
                    "avg_score": round(v["total"] / v["n"], 1) if v["n"] else 0.0}
                for k, v in type_dist.items()
            },
        }
    finally:
        if own:
            try:
                db.close()
            except Exception:
                pass


def rebuild_all_users(db=None) -> list[dict]:
    """重建全部用户统计（用于历史数据回填 / 定时任务）。"""
    own = db is None
    if own:
        from src.api.utils import get_db
        db = get_db()
    try:
        rows = db.execute("SELECT uid FROM users").fetchall()
        uids = [r[0] if not isinstance(r, dict) else r["uid"] for r in rows]
        results = []
        for uid in uids:
            r = rebuild_user_stats(uid, db=db)
            if r["total_practice"] > 0:
                results.append(r)
        if own:
            db.commit()
        return results
    finally:
        if own:
            try:
                db.close()
            except Exception:
                pass


def get_profile_stats(uid: str, db=None) -> dict:
    """给前端返回一份完整、可直接渲染的个人档案。

    返回结构包含 has_enough_data 判定，前端据此决定渲染图表还是空状态引导，
    从根本上避免"图表区域一片空白"。
    """
    own = db is None
    if own:
        from src.api.utils import get_db
        db = get_db()
    try:
        practices = collect_practices(db, uid)
        abilities = calc_abilities(practices)
        total = len(practices)

        # 每日趋势（近 30 天）
        # 说明：count 计入全部练习行为；avg_score 只统计有效（已评分）记录，
        #       避免"未评分=0 分"把均分拉低。
        daily: dict[str, dict] = defaultdict(lambda: {"count": 0, "total": 0.0, "n": 0})
        for p in practices:
            d = (p.get("created_at") or "")[:10]
            if not d:
                continue
            daily[d]["count"] += 1
            if p.get("valid") is not False and p.get("score") is not None:
                daily[d]["total"] += float(p["score"])
                daily[d]["n"] += 1
        trend = [{"date": d, "count": v["count"],
                  "avg_score": round(v["total"] / v["n"], 1) if v["n"] else 0.0}
                 for d, v in sorted(daily.items())[-30:]]

        # 题型分布（同样区分练习量与有效均分）
        type_dist: dict[str, dict] = {}
        for p in practices:
            qt = p.get("qtype") or (_guess_type(p["dims"]) if p.get("dims") else "")
            if not qt:
                continue
            acc = type_dist.setdefault(qt, {"count": 0, "total": 0.0, "n": 0})
            acc["count"] += 1
            if p.get("valid") is not False and p.get("score") is not None:
                acc["total"] += float(p["score"])
                acc["n"] += 1

        # 薄弱项
        weak = sync_weak_points(db, uid) if total >= THRESHOLDS["weakness"] else []

        # 本周做题量（按题型统计）。以最近 7 个自然日为窗口。
        from datetime import datetime as _dt, timedelta as _td
        week_ago = (_dt.now() - _td(days=7)).date().isoformat()
        week_total = 0
        week_by_type: dict[str, int] = {}
        for p in practices:
            d = (p.get("created_at") or "")[:10]
            if not d or d < week_ago:
                continue
            qt = p.get("qtype") or (_guess_type(p["dims"]) if p.get("dims") else "")
            week_total += 1
            if qt:
                week_by_type[qt] = week_by_type.get(qt, 0) + 1
        week_by_type = {TYPE_LABELS.get(k, k): v for k, v in week_by_type.items()}

        # 连续天数
        streak_row = db.execute(
            "SELECT streak FROM daily_practice_streaks WHERE uid=?", (uid,)
        ).fetchone()
        streak = streak_row[0] if streak_row else 0

        has_enough = {
            "radar": total >= THRESHOLDS["radar"],
            "type_matrix": total >= THRESHOLDS["type_matrix"],
            "trend": len(trend) >= THRESHOLDS["trend"],
            "weakness": total >= THRESHOLDS["weakness"],
        }

        return {
            "total_practice": total,
            "streak_days": streak or 0,
            "abilities": {ABILITY_LABELS[k]: v for k, v in abilities.items()},
            "ability_keys": abilities,
            "trend": trend,
            "type_distribution": {
                TYPE_LABELS.get(k, k): {
                    "key": k,
                    "count": v["count"],
                    "avg_score": round(v["total"] / v["n"], 1) if v["n"] else 0.0,
                } for k, v in type_dist.items()
            },
            "weak_points": weak,
            "week_practice": {
                "total": week_total,
                "by_type": week_by_type,
            },
            "has_enough_data": has_enough,
            "thresholds": THRESHOLDS,
        }
    finally:
        if own:
            try:
                db.close()
            except Exception:
                pass
