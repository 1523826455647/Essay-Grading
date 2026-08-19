# Token 消耗记录服务
#
# 记录每次 LLM 调用的 prompt_tokens / completion_tokens，
# 按模型配置的每百万 token 价格计算成本，支持按天/按模型统计。

import json
import logging
import sqlite3
import os

from src.config import Config

logger = logging.getLogger(__name__)


def _get_db():
    """直接打开数据库连接（不依赖 Flask g，线程安全）。"""
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    return db


def record_usage(model_id, model_name, prompt_tokens, completion_tokens, source='grading', sid=None):
    """记录一次 LLM 调用的 token 消耗并计算成本

    Args:
        model_id: 模型 ID（用于查价格）
        model_name: 模型显示名
        prompt_tokens: 输入 token 数
        completion_tokens: 输出 token 数
        source: 来源（grading 批改 / analyze 素材分析 / other）
        sid: 关联的批改记录 ID（可选）
    """
    prompt_tokens = int(prompt_tokens or 0)
    completion_tokens = int(completion_tokens or 0)
    if prompt_tokens == 0 and completion_tokens == 0:
        return

    db = _get_db()
    input_price = 0.0
    output_price = 0.0
    if model_id:
        row = db.execute(
            "SELECT input_price_per_mtok, output_price_per_mtok FROM llm_models WHERE model_id = ?",
            (model_id,),
        ).fetchone()
        if row:
            input_price = float(row['input_price_per_mtok'] or 0)
            output_price = float(row['output_price_per_mtok'] or 0)

    total_tokens = prompt_tokens + completion_tokens
    # 成本 = (输入token × 输入价 + 输出token × 输出价) / 1_000_000
    cost = (prompt_tokens * input_price + completion_tokens * output_price) / 1_000_000.0

    db.execute(
        """INSERT INTO token_usage_logs
           (model_id, model_name, prompt_tokens, completion_tokens, total_tokens,
            input_price, output_price, cost, source, sid, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        (model_id, model_name, prompt_tokens, completion_tokens, total_tokens,
         input_price, output_price, round(cost, 6), source, sid),
    )
    db.commit()


def get_submission_usage(sid):
    """获取某次批改的 token 消耗汇总（按模型分组）。

    Returns:
        dict: {
            models: [{model_name, prompt_tokens, completion_tokens, total_tokens, cost}],
            total_tokens: int,
            total_cost: float,
        }
    """
    db = _get_db()
    rows = db.execute(
        """SELECT model_name, SUM(prompt_tokens) AS prompt_tokens,
                  SUM(completion_tokens) AS completion_tokens,
                  SUM(total_tokens) AS total_tokens, SUM(cost) AS cost
           FROM token_usage_logs WHERE sid = ?
           GROUP BY model_name ORDER BY cost DESC""",
        (sid,),
    ).fetchall()

    models = [
        {
            'model_name': r['model_name'] or '未知模型',
            'prompt_tokens': int(r['prompt_tokens'] or 0),
            'completion_tokens': int(r['completion_tokens'] or 0),
            'total_tokens': int(r['total_tokens'] or 0),
            'cost': round(float(r['cost'] or 0), 6),
        }
        for r in rows
    ]

    return {
        'models': models,
        'total_tokens': sum(m['total_tokens'] for m in models),
        'total_cost': round(sum(m['cost'] for m in models), 6),
    }


def get_usage_summary(days=7):
    """获取近 N 天的 token 消耗统计

    Returns:
        dict: {total_tokens, total_cost, daily: [...], by_model: [...]}
    """
    db = _get_db()
    window = f'-{int(days)} days'

    total_row = db.execute(
        "SELECT COALESCE(SUM(total_tokens),0) AS tokens, COALESCE(SUM(cost),0) AS cost "
        "FROM token_usage_logs WHERE created_at >= datetime('now', ?)",
        (window,),
    ).fetchone()

    daily_rows = db.execute(
        "SELECT date(created_at) AS day, COALESCE(SUM(total_tokens),0) AS tokens, "
        "COALESCE(SUM(cost),0) AS cost, COUNT(*) AS calls "
        "FROM token_usage_logs WHERE created_at >= datetime('now', ?) "
        "GROUP BY date(created_at) ORDER BY day DESC",
        (window,),
    ).fetchall()

    model_rows = db.execute(
        "SELECT COALESCE(model_name, '未知模型') AS model_name, "
        "COALESCE(SUM(total_tokens),0) AS tokens, COALESCE(SUM(cost),0) AS cost, COUNT(*) AS calls "
        "FROM token_usage_logs WHERE created_at >= datetime('now', ?) "
        "GROUP BY model_name ORDER BY cost DESC",
        (window,),
    ).fetchall()

    return {
        'total_tokens': int(total_row['tokens'] or 0),
        'total_cost': round(float(total_row['cost'] or 0), 4),
        'days': int(days),
        'daily': [
            {
                'day': r['day'],
                'tokens': int(r['tokens'] or 0),
                'cost': round(float(r['cost'] or 0), 4),
                'calls': int(r['calls'] or 0),
            }
            for r in daily_rows
        ],
        'by_model': [
            {
                'model_name': r['model_name'],
                'tokens': int(r['tokens'] or 0),
                'cost': round(float(r['cost'] or 0), 4),
                'calls': int(r['calls'] or 0),
            }
            for r in model_rows
        ],
    }


def get_usage_records(page=1, per_page=20, source=None, model_name=None):
    """获取 token 消耗明细（每次 LLM 调用一条），支持分页和按来源/模型过滤。

    Returns:
        dict: {records: [...], total, page, per_page, pages}
    """
    db = _get_db()
    where = []
    params = []
    if source:
        where.append("source = ?")
        params.append(source)
    if model_name:
        where.append("model_name = ?")
        params.append(model_name)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total = db.execute(
        f"SELECT COUNT(*) FROM token_usage_logs{where_sql}", params
    ).fetchone()[0]

    per_page = max(1, min(int(per_page), 200))
    page = max(1, int(page))
    offset = (page - 1) * per_page

    rows = db.execute(
        f"""SELECT id, model_name, prompt_tokens, completion_tokens, total_tokens,
                   cost, source, sid, created_at
            FROM token_usage_logs{where_sql}
            ORDER BY id DESC LIMIT ? OFFSET ?""",
        params + [per_page, offset],
    ).fetchall()

    records = [
        {
            'id': r['id'],
            'model_name': r['model_name'] or '未知模型',
            'source': r['source'] or 'other',
            'sid': r['sid'] or '',
            'prompt_tokens': int(r['prompt_tokens'] or 0),
            'completion_tokens': int(r['completion_tokens'] or 0),
            'total_tokens': int(r['total_tokens'] or 0),
            'cost': round(float(r['cost'] or 0), 6),
            'created_at': r['created_at'],
        }
        for r in rows
    ]

    return {
        'records': records,
        'total': int(total),
        'page': page,
        'per_page': per_page,
        'pages': (int(total) + per_page - 1) // per_page if total else 0,
    }
