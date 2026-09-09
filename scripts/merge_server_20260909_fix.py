# -*- coding: utf-8 -*-
"""合并补漏：处理 merge_server_20260909.py 未覆盖的表。

背景：首轮合并的表清单来源于 A(8.17) 的 sqlite_master，遗漏了服务器侧
新增的表（submission_judgments / tickets / ticket_replies / chat_messages /
packages / user_packages / essay_anchors / question_tags）。本脚本在
merged.db 上补齐这些表的 B 新增行，并做孤儿判定清理。

用法：python scripts/merge_server_20260909_fix.py   （在项目根目录执行）
"""
import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
B_PATH = os.path.join(ROOT, "data", "merge-20260909", "server-20260909.db")
MERGED_PATH = os.path.join(ROOT, "data", "merge-20260909", "merged.db")


def run(m, sql, params=()):
    return m.execute(sql, params).rowcount


def one(m, sql, params=()):
    return m.execute(sql, params).fetchone()[0]


def main():
    assert os.path.exists(MERGED_PATH), "请先运行 merge_server_20260909.py 生成 merged.db"
    m = sqlite3.connect(MERGED_PATH)
    m.execute("PRAGMA foreign_keys = OFF")
    m.execute("ATTACH DATABASE ? AS bdb", (B_PATH,))

    # ---- 1. submission_judgments（TEXT 主键，B 与 C 无重叠，直接并入）----
    n = run(
        m,
        "INSERT OR IGNORE INTO main.submission_judgments "
        "SELECT * FROM bdb.submission_judgments "
        "WHERE judgment_id NOT IN (SELECT judgment_id FROM main.submission_judgments)",
    )
    m.commit()
    print(f"[submission_judgments] 并入B行 {n}，现共 {one(m, 'SELECT COUNT(*) FROM main.submission_judgments')}")

    # ---- 2. 服务器新增的表：tickets / ticket_replies / chat_messages ----
    for t, pk in [("tickets", "id"), ("ticket_replies", "id"), ("chat_messages", "id")]:
        if not one(m, "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (t,)):
            print(f"[{t}] main 无此表，跳过")
            continue
        n = run(
            m,
            f"INSERT OR IGNORE INTO main.{t} "
            f"SELECT * FROM bdb.{t} "
            f"WHERE {pk} NOT IN (SELECT {pk} FROM main.{t})",
        )
        m.commit()
        print(f"[{t}] 并入B新增 {n} 行")

    # ---- 3. packages / user_packages / essay_anchors / question_tags ----
    for t, pk in [("packages", "id"), ("user_packages", "id"),
                  ("question_tags", "id")]:
        n = run(
            m,
            f"INSERT OR IGNORE INTO main.{t} "
            f"SELECT * FROM bdb.{t} "
            f"WHERE {pk} NOT IN (SELECT {pk} FROM main.{t})",
        )
        m.commit()
        print(f"[{t}] 并入B新增 {n} 行")

    # essay_anchors：复合主键 (anchor_key, model_id)
    n = run(
        m,
        "INSERT OR IGNORE INTO main.essay_anchors "
        "SELECT * FROM bdb.essay_anchors "
        "WHERE anchor_key NOT IN (SELECT anchor_key FROM main.essay_anchors)",
    )
    m.commit()
    print(f"[essay_anchors] 并入B新增 {n} 行")

    # ---- 4. 孤儿判定清理（sid 不在合并后 submissions 的判定）----
    n = run(
        m,
        "DELETE FROM main.submission_judgments "
        "WHERE sid NOT IN (SELECT sid FROM main.submissions)",
    )
    m.commit()
    total = one(m, "SELECT COUNT(*) FROM main.submission_judgments")
    print(f"[孤儿清理] 删除判定 {n} 条，现共 {total}（对应 {one(m, 'SELECT COUNT(DISTINCT sid) FROM main.submission_judgments')} 套批改）")

    m.close()
    print("补漏完成")


if __name__ == "__main__":
    main()
