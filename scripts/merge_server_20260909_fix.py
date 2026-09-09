# -*- coding: utf-8 -*-
"""合并补漏（历史补丁，已并入 merge_server_20260909.py 主脚本）。

处理首轮合并遗漏的服务器新增表：submission_judgments / tickets /
ticket_replies / chat_messages / packages / user_packages / essay_anchors /
question_tags。主脚本现已包含同等逻辑（全静态 SQL），本文件保留作为
2026-09-09 合并操作的历史记录；重复执行幂等（INSERT OR IGNORE）。

用法：python scripts/merge_server_20260909_fix.py   （在项目根目录执行）
"""
import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
B_PATH = os.path.join(ROOT, "data", "merge-20260909", "server-20260909.db")
MERGED_PATH = os.path.join(ROOT, "data", "merge-20260909", "merged.db")


def main():
    assert os.path.exists(MERGED_PATH), "请先运行 merge_server_20260909.py 生成 merged.db"
    m = sqlite3.connect(MERGED_PATH)
    m.execute("PRAGMA foreign_keys = OFF")
    m.execute("ATTACH DATABASE ? AS bdb", (B_PATH,))

    # 1. submission_judgments（TEXT 主键，无重叠直接并入）
    m.execute(
        "INSERT OR IGNORE INTO main.submission_judgments "
        "SELECT * FROM bdb.submission_judgments "
        "WHERE judgment_id NOT IN (SELECT judgment_id FROM main.submission_judgments)"
    )
    m.commit()

    # 2. tickets / ticket_replies / chat_messages：按 id 并入
    m.execute(
        "INSERT OR IGNORE INTO main.tickets "
        "SELECT * FROM bdb.tickets WHERE id NOT IN (SELECT id FROM main.tickets)"
    )
    m.execute(
        "INSERT OR IGNORE INTO main.ticket_replies "
        "SELECT * FROM bdb.ticket_replies WHERE id NOT IN (SELECT id FROM main.ticket_replies)"
    )
    m.execute(
        "INSERT OR IGNORE INTO main.chat_messages "
        "SELECT * FROM bdb.chat_messages WHERE id NOT IN (SELECT id FROM main.chat_messages)"
    )
    m.commit()

    # 3. packages / user_packages / question_tags：按 id 并入
    m.execute(
        "INSERT OR IGNORE INTO main.packages "
        "SELECT * FROM bdb.packages WHERE id NOT IN (SELECT id FROM main.packages)"
    )
    m.execute(
        "INSERT OR IGNORE INTO main.user_packages "
        "SELECT * FROM bdb.user_packages WHERE id NOT IN (SELECT id FROM main.user_packages)"
    )
    if not m.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='question_tags'"
    ).fetchone()[0]:
        ddl = m.execute(
            "SELECT sql FROM bdb.sqlite_master WHERE type='table' AND name='question_tags'"
        ).fetchone()[0]
        m.execute(ddl)
        for idx in m.execute(
            "SELECT sql FROM bdb.sqlite_master WHERE type='index' "
            "AND tbl_name='question_tags' AND sql IS NOT NULL"
        ).fetchall():
            m.execute(idx[0])
    m.execute(
        "INSERT OR IGNORE INTO main.question_tags "
        "SELECT * FROM bdb.question_tags WHERE id NOT IN (SELECT id FROM main.question_tags)"
    )
    m.commit()

    # 4. essay_anchors：复合主键并入
    m.execute(
        "INSERT OR IGNORE INTO main.essay_anchors "
        "SELECT * FROM bdb.essay_anchors "
        "WHERE anchor_key NOT IN (SELECT anchor_key FROM main.essay_anchors)"
    )
    m.commit()

    # 5. 孤儿判定清理
    m.execute(
        "DELETE FROM main.submission_judgments "
        "WHERE sid NOT IN (SELECT sid FROM main.submissions)"
    )
    m.commit()

    total = m.execute("SELECT COUNT(*) FROM main.submission_judgments").fetchone()[0]
    print(f"补漏完成，submission_judgments 现共 {total} 条")
    m.close()


if __name__ == "__main__":
    main()
