# 题型训练服务
#
# 五种题型（归纳概括/综合分析/提出对策/贯彻执行/大作文）
# 的训练统计、推荐、历史和进步趋势

import json
import re
from datetime import datetime
from src.api.utils import get_db, generate_uuid
from src.services.grader.rubric import normalize_question_type

# 题型代码与中文名映射
QUESTION_TYPE_NAMES = {
    'guina': '归纳概括',
    'zonghe': '综合分析',
    'duice': '提出对策',
    'zhixing': '贯彻执行',
    'zuowen': '大作文'
}

# 段位体系（六大段位：青铜→白银→黄金→铂金→钻石→王者）
# 每个段位按段位内练习量细分 I / II / III 子段位（I 最高），
# 0 次练习返回 None（未定级），不再把未练过的题型显示成青铜。
LEVEL_THRESHOLDS = [
    # (段位代码, 最低练习次数, 最低均分)，从高到低
    ('king',     50, 90),
    ('diamond',  20, 85),
    ('platinum', 10, 80),
    ('gold',      5, 70),
    ('silver',    3, 60),
    ('bronze',    1, 0),
]

LEVEL_NAMES = {
    'bronze': '青铜',
    'silver': '白银',
    'gold': '黄金',
    'platinum': '铂金',
    'diamond': '钻石',
    'king': '王者',
}

SUB_LEVEL_NAMES = {3: 'III', 2: 'II', 1: 'I'}


def _calculate_level(avg_score, total_attempts):
    """根据均分和练习次数计算段位代码；0 次练习返回 None（未定级）。"""
    if not total_attempts or total_attempts < 1:
        return None
    for level, min_attempts, min_score in LEVEL_THRESHOLDS:
        if total_attempts >= min_attempts and avg_score >= min_score:
            return level
    return 'bronze'


def _sub_level(tier: str, total_attempts: int) -> int:
    """段位内子段位：按段位内练习量推进 III → II → I。"""
    if tier == 'king':
        # 王者段按额外练习量细分：50+ → III，70+ → II，90+ → I
        if total_attempts >= 90:
            return 1
        if total_attempts >= 70:
            return 2
        return 3
    order = [t for t, _, _ in LEVEL_THRESHOLDS]
    idx = order.index(tier)
    cur_min = LEVEL_THRESHOLDS[idx][1]
    next_min = LEVEL_THRESHOLDS[idx - 1][1]  # 上一级段位的最低练习次数
    span = next_min - cur_min
    if span <= 1:
        return 3
    pos = total_attempts - cur_min
    if pos >= span * 2 / 3:
        return 1
    if pos >= span / 3:
        return 2
    return 3


def _level_display(tier, total_attempts: int) -> str:
    """段位展示名，如 白银II；未定级返回 '未定级'。"""
    if not tier:
        return '未定级'
    base = LEVEL_NAMES.get(tier, tier)
    return f"{base}{SUB_LEVEL_NAMES[_sub_level(tier, total_attempts)]}"


def get_user_type_stats(uid):
    """获取用户五种题型的统计数据

    Returns:
        dict: {question_type: {total_attempts, avg_score, best_score, level, ...}}
    """
    db = get_db()
    rows = db.execute(
        "SELECT * FROM user_question_type_stats WHERE uid = ?",
        (uid,)
    ).fetchall()

    stats = {}
    for row in rows:
        stats[row['question_type']] = {
            'total_attempts': row['total_attempts'],
            'avg_score': round(row['avg_score'], 1),
            'best_score': round(row['best_score'], 1),
            'level': row['level'],
            'level_name': _level_display(row['level'], row['total_attempts']),
            'dimension_breakdown': json.loads(row['dimension_breakdown']) if row['dimension_breakdown'] else {},
            'last_attempt_at': row['last_attempt_at']
        }

    # 补充未练习过的题型
    for qtype in QUESTION_TYPE_NAMES:
        if qtype not in stats:
            stats[qtype] = {
                'total_attempts': 0,
                'avg_score': 0,
                'best_score': 0,
                'level': None,
                'level_name': '未定级',
                'dimension_breakdown': {},
                'last_attempt_at': None
            }

    return stats


def record_drill(uid, question_type, pid, qid, sid, score, dimension_scores=None, time_spent=None):
    """记录一次题型训练

    同时更新 user_question_type_stats 汇总表
    """
    db = get_db()
    # Always store internal type codes (guina/zonghe/...), never raw Chinese labels.
    question_type = normalize_question_type(question_type)

    # 计算踩点率
    hit_rate = 0.0
    if dimension_scores:
        point_cov = dimension_scores.get('point_coverage', dimension_scores.get('踩点命中', 0))
        max_cov = 70 if 'point_coverage' in dimension_scores else 40
        hit_rate = point_cov / max_cov if max_cov > 0 else 0

    # 插入训练记录
    db.execute(
        """INSERT INTO question_type_drills
           (uid, question_type, pid, qid, sid, score, dimension_scores, key_point_hit_rate, time_spent)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (uid, question_type, pid, qid, sid, score,
         json.dumps(dimension_scores) if dimension_scores else None,
         hit_rate, time_spent)
    )

    # 更新汇总统计
    existing = db.execute(
        "SELECT * FROM user_question_type_stats WHERE uid = ? AND question_type = ?",
        (uid, question_type)
    ).fetchone()

    now = datetime.now().isoformat()

    if existing:
        new_attempts = existing['total_attempts'] + 1
        new_total = existing['total_score'] + score
        new_avg = new_total / new_attempts
        new_best = max(existing['best_score'], score)
        new_level = _calculate_level(new_avg, new_attempts)

        db.execute(
            """UPDATE user_question_type_stats
               SET total_attempts = ?, total_score = ?, avg_score = ?,
                   best_score = ?, level = ?, last_attempt_at = ?,
                   dimension_breakdown = ?, updated_at = ?
               WHERE uid = ? AND question_type = ?""",
            (new_attempts, new_total, round(new_avg, 2), new_best,
             new_level, now,
             json.dumps(dimension_scores) if dimension_scores else existing['dimension_breakdown'],
             now, uid, question_type)
        )
    else:
        new_level = _calculate_level(score, 1)
        db.execute(
            """INSERT INTO user_question_type_stats
               (uid, question_type, total_attempts, total_score, avg_score,
                best_score, last_attempt_at, dimension_breakdown, level)
               VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)""",
            (uid, question_type, score, score, score, now,
             json.dumps(dimension_scores) if dimension_scores else '{}',
             new_level)
        )

    # 同步个人档案统计（打通题型训练 -> 学习概况）
    # 统计写入 user_dimension_trends / daily_practice / user_weak_points，
    # 与本次练习记录在同一事务中提交。
    try:
        from src.services.profile_stats_service import rebuild_user_stats
        rebuild_user_stats(uid, db=db)
    except Exception as _e:  # 统计失败绝不能影响主流程
        try:
            print(f"[drill] 档案统计同步失败: {_e}")
        except Exception:
            pass

    db.commit()


def get_drill_history(uid, question_type=None, page=1, per_page=20):
    """获取训练历史"""
    db = get_db()
    offset = (page - 1) * per_page

    if question_type:
        rows = db.execute(
            """SELECT d.*, p.title as paper_title
               FROM question_type_drills d
               LEFT JOIN papers p ON d.pid = p.pid
               WHERE d.uid = ? AND d.question_type = ?
               ORDER BY d.created_at DESC LIMIT ? OFFSET ?""",
            (uid, question_type, per_page, offset)
        ).fetchall()
        total = db.execute(
            "SELECT COUNT(*) as cnt FROM question_type_drills WHERE uid = ? AND question_type = ?",
            (uid, question_type)
        ).fetchone()['cnt']
    else:
        rows = db.execute(
            """SELECT d.*, p.title as paper_title
               FROM question_type_drills d
               LEFT JOIN papers p ON d.pid = p.pid
               WHERE d.uid = ?
               ORDER BY d.created_at DESC LIMIT ? OFFSET ?""",
            (uid, per_page, offset)
        ).fetchall()
        total = db.execute(
            "SELECT COUNT(*) as cnt FROM question_type_drills WHERE uid = ?",
            (uid,)
        ).fetchone()['cnt']

    items = []
    for row in rows:
        items.append({
            'id': row['id'],
            'question_type': row['question_type'],
            'question_type_name': QUESTION_TYPE_NAMES.get(row['question_type'], ''),
            'pid': row['pid'],
            'qid': row['qid'],
            'sid': row['sid'],
            'score': row['score'],
            'dimension_scores': json.loads(row['dimension_scores']) if row['dimension_scores'] else None,
            'key_point_hit_rate': row['key_point_hit_rate'],
            'time_spent': row['time_spent'],
            'paper_title': row['paper_title'],
            'created_at': row['created_at']
        })

    return {
        'items': items,
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': (total + per_page - 1) // per_page
    }


def get_drill_progress(uid, question_type, limit=10):
    """获取某题型的进步趋势数据

    Returns:
        list: [{score, created_at}, ...] 按时间正序
    """
    db = get_db()
    rows = db.execute(
        """SELECT score, created_at FROM question_type_drills
           WHERE uid = ? AND question_type = ?
           ORDER BY created_at DESC LIMIT ?""",
        (uid, question_type, limit)
    ).fetchall()

    # 反转为时间正序
    return [{'score': row['score'], 'created_at': row['created_at']} for row in reversed(rows)]


def _reco_user_weakness(uid):
    """用户五维能力薄弱度 {ability: 0~1}，1 为最弱。无数据返回 {}。"""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT dim_name, avg_score FROM user_dimension_trends WHERE uid=?",
            (uid,)).fetchall()
    except Exception:
        return {}
    scores = {}
    for r in rows:
        try:
            scores[r["dim_name"]] = float(r["avg_score"] or 0)
        except (TypeError, ValueError):
            continue
    if not scores:
        return {}
    lo, hi = min(scores.values()), max(scores.values())
    span = (hi - lo) or 1.0
    return {k: round(1.0 - (v - lo) / span, 3) for k, v in scores.items()}


def _reco_year_of(pid, title):
    m = re.search(r"(20\d{2})", str(pid or "") + str(title or ""))
    return int(m.group(1)) if m else None


def _reco_year_score(y, current=2026):
    if not y:
        return 0.3
    age = max(0, current - y)
    if age <= 1:
        return 1.0
    if age <= 3:
        return 0.9
    if age <= 5:
        return 0.75
    if age <= 8:
        return 0.5
    return 0.25


def get_recommended_questions_v2(uid, question_type, limit=5, sub_filter=None):
    """基于侧重标签的针对性推荐（question_tags 由 LongCat-2.0 批量分析生成）。"""
    db = get_db()
    weakness = _reco_user_weakness(uid)

    done = db.execute(
        "SELECT DISTINCT pid, qid FROM question_type_drills "
        "WHERE uid = ? AND question_type = ?",
        (uid, question_type)).fetchall()
    done_set = {(r["pid"], r["qid"]) for r in done}

    # 用户各二级细分练习次数（覆盖均衡用）
    sub_count = {}
    try:
        rows = db.execute(
            """SELECT t.sub_type AS st, COUNT(*) AS n
               FROM question_type_drills d
               JOIN question_tags t ON t.pid = d.pid AND t.qid = d.qid
               WHERE d.uid = ? GROUP BY t.sub_type""", (uid,)).fetchall()
        sub_count = {r["st"]: r["n"] for r in rows}
    except Exception:
        pass
    max_sub = max(sub_count.values()) if sub_count else 0

    papers = db.execute(
        "SELECT pid, title, questions, difficulty FROM papers "
        "WHERE status = 'published'").fetchall()
    cands = []
    for paper in papers:
        try:
            qs = json.loads(paper["questions"]) if paper["questions"] else []
        except Exception:
            continue
        ys = _reco_year_score(_reco_year_of(paper["pid"], paper["title"]))
        for q in qs:
            qtype = normalize_question_type(
                q.get("type"), q.get("stem", q.get("question_text", "")))
            if qtype != question_type:
                continue
            cands.append({
                "pid": paper["pid"],
                "qid": str(q.get("qid", "")),
                "paper_title": paper["title"],
                "question_text": q.get("stem", q.get("question_text", "")),
                "difficulty": paper["difficulty"],
                "word_limit": q.get("word_limit", ""),
                "year_score": ys,
                "done": (paper["pid"], str(q.get("qid", ""))) in done_set,
            })
    if not cands:
        return []

    # 批量取标签
    tag_map = {}
    try:
        pids = list({c["pid"] for c in cands})
        for i in range(0, len(pids), 200):
            chunk = pids[i:i + 200]
            ph = ",".join("?" * len(chunk))
            rows = db.execute(
                "SELECT pid, qid, sub_type, focus_ability FROM question_tags "
                f"WHERE pid IN ({ph})", chunk).fetchall()
            for r in rows:
                tag_map[(r["pid"], r["qid"])] = (r["sub_type"], r["focus_ability"])
    except Exception:
        pass

    for c in cands:
        tag = tag_map.get((c["pid"], c["qid"]))
        if tag:
            sub, ab = tag
            c["sub_type"] = sub
            if sub_filter and sub != sub_filter:
                c["_drop"] = True
                continue
            ability_s = weakness.get(ab, 0.5) if weakness else 0.5
            cover_s = (1.0 - sub_count.get(sub, 0) / max_sub) if max_sub else 1.0
        else:
            c["sub_type"] = None
            if sub_filter:
                c["_drop"] = True
                continue
            ability_s, cover_s = 0.5, 0.5
        novelty = 0.0 if c["done"] else 1.0
        c["score"] = round(0.40 * ability_s + 0.25 * cover_s
                           + 0.20 * c["year_score"] + 0.15 * novelty, 4)

    cands = [c for c in cands if not c.get("_drop")]
    cands.sort(key=lambda x: (-x["score"],
                              abs((x["difficulty"] or 3) - 3)))
    return cands[:limit]


def get_recommended_questions(uid, question_type, limit=5, sub_filter=None):
    """推荐练习题（v2：侧重标签针对性；异常时回退旧逻辑保底）。"""
    try:
        result = get_recommended_questions_v2(uid, question_type, limit,
                                              sub_filter=sub_filter)
        if result:
            return result
    except Exception as _e:
        try:
            print(f"[drill] v2 推荐异常，回退旧逻辑: {_e}")
        except Exception:
            pass
    # ---- 旧逻辑兜底 ----
    db = get_db()
    done = db.execute(
        "SELECT DISTINCT pid, qid FROM question_type_drills WHERE uid = ? AND question_type = ?",
        (uid, question_type)).fetchall()
    done_set = set((r["pid"], r["qid"]) for r in done)
    papers = db.execute(
        "SELECT pid, title, questions, difficulty FROM papers WHERE status = 'published'"
    ).fetchall()
    candidates = []
    for paper in papers:
        questions = json.loads(paper["questions"]) if paper["questions"] else []
        for q in questions:
            qtype = normalize_question_type(
                q.get("type"), q.get("stem", q.get("question_text", "")))
            if qtype == question_type:
                if (paper["pid"], q.get("qid", "")) not in done_set:
                    candidates.append({
                        "pid": paper["pid"],
                        "qid": q.get("qid", ""),
                        "paper_title": paper["title"],
                        "question_text": q.get("stem", q.get("question_text", "")),
                        "difficulty": paper["difficulty"],
                        "word_limit": q.get("word_limit", "")
                    })
    candidates.sort(key=lambda x: abs(x["difficulty"] - 3))
    return candidates[:limit]