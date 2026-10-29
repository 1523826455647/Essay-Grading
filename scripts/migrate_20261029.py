# -*- coding: utf-8 -*-
"""2026-10-29 幂等迁移：自定义批改数据隔离 + 审计表。

背景：POST /api/custom-grade 每次提交都会新建一条 pid 以 custom_ 开头的
papers 记录（历史遗留 status=custom、source=用户上传）。后台试卷列表
未过滤，导致全量试卷查询被私人任务污染。

本迁移做两件事（可重复执行，第二次影响 0 行）：
  1. 建 audit_custom_grade 表（限流计数 + uid/ip/ua 审计）。
  2. 规范化历史自定义试卷标记：pid 以 custom_ 开头的行统一
     status=custom、source=custom，供所有列表默认过滤；
     不删除任何历史数据，自定义任务仍可在「自定义任务」视图查看。

用法（项目根目录）：
  python scripts/migrate_20261029.py
或容器内：
  docker exec -e PYTHONPATH=/app shenlun-ai python3 /app/scripts/migrate_20261029.py
"""
import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "slb.db")


def migrate(db_path: str) -> dict:
    """执行幂等迁移，返回统计信息（可被测试直接调用）。"""
    assert os.path.exists(db_path), f"缺少数据库文件: {db_path}"
    db = sqlite3.connect(db_path)
    db.execute("""
        CREATE TABLE IF NOT EXISTS audit_custom_grade (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            uid        TEXT NOT NULL,
            ip         TEXT NOT NULL DEFAULT '',
            ua         TEXT NOT NULL DEFAULT '',
            sid        TEXT,
            created_at DATETIME DEFAULT (datetime('now'))
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_cg_uid_time
            ON audit_custom_grade(uid, created_at)
    """)

    # 历史自定义试卷：统一标记（参数绑定，pid 前缀为常量）
    cur1 = db.execute(
        "UPDATE papers SET status = 'custom' WHERE pid LIKE ? AND status != 'custom'",
        ("custom_%",),
    )
    cur2 = db.execute(
        "UPDATE papers SET source = 'custom' WHERE pid LIKE ? AND source != 'custom'",
        ("custom_%",),
    )
    db.commit()

    total = db.execute(
        "SELECT COUNT(*) FROM papers WHERE pid LIKE ?", ("custom_%",)
    ).fetchone()[0]
    audit_exists = db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='audit_custom_grade'"
    ).fetchone()[0]
    db.close()
    return {
        "custom_total": total,
        "status_updated": cur1.rowcount,
        "source_updated": cur2.rowcount,
        "audit_table": bool(audit_exists),
    }


def main():
    stats = migrate(DB_PATH)
    print(f"[migrate] 自定义试卷 {stats['custom_total']} 条；"
          f"本次规范化 status {stats['status_updated']} 行、"
          f"source {stats['source_updated']} 行")
    print(f"[migrate] audit_custom_grade 表: {'OK' if stats['audit_table'] else 'MISSING'}")
    print("[migrate] 完成（可重复执行，第二次 UPDATE 应为 0 行）")


if __name__ == "__main__":
    main()
