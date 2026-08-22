# 能力诊断报告服务
#
# 基于用户的历史提交数据，生成结构化的诊断报告
# 包含五维度得分、五题型得分、强弱项分析、推荐建议和趋势数据

import json
from datetime import datetime, timedelta
from src.api.utils import get_db, generate_uuid

# 维度名称映射
DIMENSION_NAMES = {
    'point_coverage': '踩点命中',
    'logic_structure': '逻辑结构',
    'language': '语言表达',
    'format': '格式规范',
    'word_count': '字数控制',
    'conciseness': '语言简洁',
    'accuracy': '归纳准确',
    'logic_chain': '逻辑链完整性',
    'depth': '分析深度',
    'problem_identification': '问题定位',
    'targeting': '针对性',
    'feasibility': '可行性',
    'specificity': '具体性',
    'format_correctness': '格式正确性',
    'purpose_achievement': '目的达成度',
    'content_completeness': '内容完整性',
    'language_appropriateness': '语言得体性',
    'thesis_accuracy': '立意准确度',
    'argument_richness': '论证充实度',
    'structure': '结构完整性',
    'innovation': '创新亮点'
}

QUESTION_TYPE_NAMES = {
    'guina': '归纳概括',
    'zonghe': '综合分析',
    'duice': '提出对策',
    'zhixing': '贯彻执行',
    'zuowen': '大作文'
}

# 统一能力画像：canonical 维度 → (中文名, {原始维度键: 满分})
# 各题型实际维度按「得分/满分」归一化为 0-100 百分比聚合，保证跨题型可比、始终有值
DIM_GROUPS = {
    'content': ('内容覆盖', {
        'point_coverage': 70, 'content_completeness': 30,
        'purpose_achievement': 25, 'problem_identification': 20,
    }),
    'logic': ('逻辑分析', {
        'logic_chain': 30, 'logic_structure': 25, 'depth': 20, 'structure': 20,
        'targeting': 25, 'specificity': 20, 'feasibility': 25,
    }),
    'language': ('语言表达', {
        'language': 20, 'language_appropriateness': 15,
        'conciseness': 15, 'accuracy': 10,
    }),
    'format': ('格式规范', {
        'format': 10, 'format_correctness': 20, 'word_count': 10,
    }),
    'essay': ('立意论证', {
        'thesis_accuracy': 25, 'argument_richness': 25, 'innovation': 10,
    }),
}

# 统一画像维度展示顺序
CANONICAL_ORDER = ['content', 'logic', 'language', 'format', 'essay']

# canonical 维度 → 推荐练习题型（闭环深链）
DIM_TO_TYPE = {
    'content': 'guina', 'logic': 'zonghe', 'language': 'guina',
    'format': 'zhixing', 'essay': 'zuowen',
}


def _canonical_dims(dim_avg: dict) -> dict:
    """把各题型实际维度归一化为统一能力画像（0-100 百分比）。

    每个画像维度 = 其下已有原始维度 得分/满分 的平均百分比；
    没有任何原始数据的维度返回 None（前端显示 --，不显示 0）。
    """
    result = {}
    for key, (_label, group) in DIM_GROUPS.items():
        ratios = []
        for dim_key, max_val in group.items():
            raw = dim_avg.get(dim_key)
            if isinstance(raw, (int, float)) and max_val > 0:
                ratios.append(max(0.0, min(float(raw) / max_val, 1.0)))
        result[key] = round(sum(ratios) / len(ratios) * 100, 1) if ratios else None
    return result


def generate_diagnostic_report(uid, sid=None):
    """基于历史数据生成诊断报告

    Args:
        uid: 用户 ID
        sid: 触发报告的提交 ID（可选）

    Returns:
        dict: 诊断报告数据
    """
    db = get_db()

    # 获取最近 10 次提交
    recent = db.execute(
        """SELECT score, dimension_scores, created_at
           FROM submissions WHERE uid = ? AND score IS NOT NULL
           ORDER BY created_at DESC LIMIT 10""",
        (uid,)
    ).fetchall()

    if not recent:
        return None

    # 1. 计算五维度平均分
    dim_accum = {}
    dim_count = {}
    for row in recent:
        dims = json.loads(row['dimension_scores']) if row['dimension_scores'] else {}
        for k, v in dims.items():
            if isinstance(v, (int, float)):
                dim_accum[k] = dim_accum.get(k, 0) + v
                dim_count[k] = dim_count.get(k, 0) + 1

    dim_avg = {}
    for k in dim_accum:
        dim_avg[k] = round(dim_accum[k] / dim_count[k], 1) if dim_count[k] > 0 else 0

    # 2. 获取五题型得分
    type_stats = db.execute(
        "SELECT question_type, avg_score FROM user_question_type_stats WHERE uid = ?",
        (uid,)
    ).fetchall()
    type_scores = {row['question_type']: round(row['avg_score'], 1) for row in type_stats}

    # 3. 计算总分
    scores = [row['score'] for row in recent]
    overall = round(sum(scores) / len(scores), 1) if scores else 0

    # 4. 识别强弱项（统一能力画像 + 题型）
    canonical = _canonical_dims(dim_avg)
    strengths = []
    weaknesses = []

    # 基于统一能力画像（百分比）
    for ck in CANONICAL_ORDER:
        val = canonical.get(ck)
        if val is None:
            continue
        label = DIM_GROUPS[ck][0]
        if val >= 70:
            strengths.append(f"{label}（{val}%）")
        elif val < 50:
            weaknesses.append(f"{label}（{val}%）")

    # 基于题型
    for qtype, avg in type_scores.items():
        name = QUESTION_TYPE_NAMES.get(qtype, qtype)
        if avg >= 75:
            strengths.append(f"{name}题型（均分{avg}）")
        elif avg < 55:
            weaknesses.append(f"{name}题型（均分{avg}）")

    # 5. 生成推荐（带练题深链，形成"诊断→练题→再诊断"闭环）
    recommendations = []
    weakest_type = None
    if type_scores:
        weakest_type = min(type_scores, key=type_scores.get)
        weakest_name = QUESTION_TYPE_NAMES.get(weakest_type, weakest_type)
        recommendations.append({
            'type': 'drill',
            'question_type': weakest_type,
            'action': f'重点练习{weakest_name}题型',
            'priority': 'high',
            'link': f'/drill?type={weakest_type}',
        })

    # 最弱能力画像维度 → 映射到对应题型练
    weakest_canonical = None
    for ck in CANONICAL_ORDER:
        val = canonical.get(ck)
        if val is None:
            continue
        if weakest_canonical is None or val < canonical.get(weakest_canonical):
            weakest_canonical = ck
    if weakest_canonical and canonical.get(weakest_canonical) is not None:
        dim_label = DIM_GROUPS[weakest_canonical][0]
        practice_type = DIM_TO_TYPE.get(weakest_canonical, weakest_type or 'guina')
        recommendations.append({
            'type': 'dimension',
            'dimension': weakest_canonical,
            'action': f'提升{dim_label}能力',
            'priority': 'medium',
            'link': f'/drill?type={practice_type}',
        })

    # 6. 趋势数据
    trend = [round(row['score'], 1) for row in recent]
    trend.reverse()  # 时间正序

    # 7. 保存报告
    report_id = generate_uuid()
    db.execute(
        """INSERT INTO diagnostic_reports
           (uid, report_type, trigger_id,
            score_point_coverage, score_logic_structure, score_language,
            score_format, score_word_count,
            score_guina, score_zonghe, score_duice, score_zhixing, score_zuowen,
            overall_score, strengths, weaknesses, recommendations, score_trend,
            dimension_scores_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (uid, 'single', sid,
         canonical.get('content'), canonical.get('logic'), canonical.get('language'),
         canonical.get('format'), None,
         type_scores.get('guina'), type_scores.get('zonghe'),
         type_scores.get('duice'), type_scores.get('zhixing'), type_scores.get('zuowen'),
         overall,
         json.dumps(strengths, ensure_ascii=False),
         json.dumps(weaknesses, ensure_ascii=False),
         json.dumps(recommendations, ensure_ascii=False),
         json.dumps(trend),
         json.dumps(canonical, ensure_ascii=False))
    )
    db.commit()

    return {
        'report_id': report_id,
        'report_type': 'single',
        'overall_score': overall,
        'dimension_scores': canonical,
        'type_scores': {k: {'score': v, 'name': QUESTION_TYPE_NAMES.get(k, k)}
                        for k, v in type_scores.items()},
        'strengths': strengths,
        'weaknesses': weaknesses,
        'recommendations': recommendations,
        'score_trend': trend,
        'total_practices': len(recent)
    }


def get_latest_report(uid):
    """获取用户最新的诊断报告"""
    db = get_db()
    row = db.execute(
        "SELECT * FROM diagnostic_reports WHERE uid = ? ORDER BY created_at DESC LIMIT 1",
        (uid,)
    ).fetchone()

    if not row:
        return None

    return _format_report(row)


def get_report_by_id(uid, report_id):
    """根据 ID 获取诊断报告"""
    db = get_db()
    row = db.execute(
        "SELECT * FROM diagnostic_reports WHERE id = ? AND uid = ?",
        (report_id, uid)
    ).fetchone()

    if not row:
        return None

    return _format_report(row)


def get_score_trend(uid, limit=10):
    """获取用户得分趋势"""
    db = get_db()
    rows = db.execute(
        """SELECT score, created_at FROM submissions
           WHERE uid = ? AND score IS NOT NULL
           ORDER BY created_at DESC LIMIT ?""",
        (uid, limit)
    ).fetchall()

    items = [{'score': round(row['score'], 1), 'created_at': row['created_at']}
             for row in rows]
    items.reverse()
    return items


def get_type_score_trend(uid, question_type, limit=10):
    """获取某题型的得分趋势"""
    db = get_db()
    rows = db.execute(
        """SELECT score, created_at FROM question_type_drills
           WHERE uid = ? AND question_type = ?
           ORDER BY created_at DESC LIMIT ?""",
        (uid, question_type, limit)
    ).fetchall()

    items = [{'score': round(row['score'], 1), 'created_at': row['created_at']}
             for row in rows]
    items.reverse()
    return items


def _format_report(row):
    """将数据库行格式化为报告字典（统一能力画像优先，旧报告回退旧 5 列）。"""
    # 新报告：dimension_scores_json 存统一画像
    dims = None
    try:
        raw = row['dimension_scores_json']
    except (IndexError, KeyError):
        raw = None
    if raw:
        try:
            dims = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            dims = None
    if not dims:
        # 旧报告回退：把旧 5 列映射到画像（essay 无从还原，置 None 显示 --）
        dims = {
            'content': row['score_point_coverage'],
            'logic': row['score_logic_structure'],
            'language': row['score_language'],
            'format': row['score_format'],
            'essay': None,
        }
    return {
        'report_id': row['id'],
        'report_type': row['report_type'],
        'overall_score': row['overall_score'],
        'dimension_scores': dims,
        'type_scores': {
            'guina': row['score_guina'],
            'zonghe': row['score_zonghe'],
            'duice': row['score_duice'],
            'zhixing': row['score_zhixing'],
            'zuowen': row['score_zuowen']
        },
        'strengths': json.loads(row['strengths']) if row['strengths'] else [],
        'weaknesses': json.loads(row['weaknesses']) if row['weaknesses'] else [],
        'recommendations': json.loads(row['recommendations']) if row['recommendations'] else [],
        'score_trend': json.loads(row['score_trend']) if row['score_trend'] else [],
        'created_at': row['created_at']
    }


def generate_weekly_report(uid):
    """生成周报：汇总本周练习数据"""
    db = get_db()

    # 获取本周提交（最近7天）
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    recent = db.execute(
        """SELECT score, dimension_scores, created_at
           FROM submissions WHERE uid = ? AND score IS NOT NULL
           AND created_at >= ?
           ORDER BY created_at DESC""",
        (uid, week_ago)
    ).fetchall()

    if len(recent) < 2:
        return None

    scores = [row['score'] for row in recent]
    avg_score = round(sum(scores) / len(scores), 1)
    best_score = round(max(scores), 1)

    # 与上周对比
    prev_week_start = (datetime.now() - timedelta(days=14)).isoformat()
    prev = db.execute(
        """SELECT AVG(score) as avg_s FROM submissions
           WHERE uid = ? AND score IS NOT NULL
           AND created_at >= ? AND created_at < ?""",
        (uid, prev_week_start, week_ago)
    ).fetchone()
    prev_avg = round(prev['avg_s'], 1) if prev and prev['avg_s'] else None
    week_change = round(avg_score - prev_avg, 1) if prev_avg else None

    # 本周题型得分
    type_stats = db.execute(
        "SELECT question_type, avg_score FROM user_question_type_stats WHERE uid = ?",
        (uid,)
    ).fetchall()
    type_scores = {row['question_type']: round(row['avg_score'], 1) for row in type_stats}

    # 最佳/最弱题型
    best_type = max(type_scores, key=type_scores.get) if type_scores else None
    worst_type = min(type_scores, key=type_scores.get) if type_scores else None

    # 本周维度平均
    dim_accum = {}
    dim_count = {}
    for row in recent:
        dims = json.loads(row['dimension_scores']) if row['dimension_scores'] else {}
        for k, v in dims.items():
            if isinstance(v, (int, float)):
                dim_accum[k] = dim_accum.get(k, 0) + v
                dim_count[k] = dim_count.get(k, 0) + 1
    dim_avg = {}
    for k in dim_accum:
        dim_avg[k] = round(dim_accum[k] / dim_count[k], 1) if dim_count[k] > 0 else 0

    # 找最弱维度（统一能力画像）
    canonical = _canonical_dims(dim_avg)
    weakest_canonical = None
    for ck in CANONICAL_ORDER:
        val = canonical.get(ck)
        if val is None:
            continue
        if weakest_canonical is None or val < canonical.get(weakest_canonical):
            weakest_canonical = ck
    weakest_dim_name = DIM_GROUPS[weakest_canonical][0] if weakest_canonical else None

    report = {
        'report_type': 'weekly',
        'total_practices': len(recent),
        'avg_score': avg_score,
        'best_score': best_score,
        'week_change': week_change,
        'best_type': {'code': best_type, 'name': QUESTION_TYPE_NAMES.get(best_type, best_type),
                       'score': type_scores.get(best_type)} if best_type else None,
        'worst_type': {'code': worst_type, 'name': QUESTION_TYPE_NAMES.get(worst_type, worst_type),
                        'score': type_scores.get(worst_type)} if worst_type else None,
        'dimension_scores': canonical,
        'type_scores': {k: {'score': v, 'name': QUESTION_TYPE_NAMES.get(k, k)}
                        for k, v in type_scores.items()},
        'improvement_area': weakest_dim_name,
        'score_trend': [round(row['score'], 1) for row in reversed(recent)]
    }

    # 保存周报
    report_id = generate_uuid()
    db.execute(
        """INSERT INTO diagnostic_reports
           (uid, report_type, trigger_id,
            score_point_coverage, score_logic_structure, score_language,
            score_format, score_word_count,
            score_guina, score_zonghe, score_duice, score_zhixing, score_zuowen,
            overall_score, strengths, weaknesses, recommendations, score_trend,
            dimension_scores_json)
           VALUES (?, 'weekly', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (uid, report_id,
         canonical.get('content'), canonical.get('logic'), canonical.get('language'),
         canonical.get('format'), None,
         type_scores.get('guina'), type_scores.get('zonghe'),
         type_scores.get('duice'), type_scores.get('zhixing'), type_scores.get('zuowen'),
         avg_score,
         json.dumps([f"{best_type}题型得分最高"], ensure_ascii=False) if best_type else '[]',
         json.dumps([f"{worst_type}题型需要加强"], ensure_ascii=False) if worst_type else '[]',
         json.dumps([{'type': 'dimension', 'dimension': weakest_canonical,
                      'action': f'重点提升{weakest_dim_name}', 'priority': 'high',
                      'link': f"/drill?type={DIM_TO_TYPE.get(weakest_canonical, 'guina')}"}], ensure_ascii=False) if weakest_canonical else '[]',
         json.dumps(report['score_trend']),
         json.dumps(canonical, ensure_ascii=False))
    )
    db.commit()

    report['report_id'] = report_id
    return report
