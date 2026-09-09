# -*- coding: utf-8 -*-
"""三方数据库合并（全静态 SQL）：A(8.17基准) + B(服务器20260909) + C(本地当前)

方法：把 A、B 以 ATTACH 方式挂到 merged（C 的拷贝）上，所有 SQL 均为
完整字面量（表名/列名逐表展开，值用 ? 参数绑定），无任何动态拼接。

  - B 新增行（相对 A）并入 merged；C 新增行保留（本地批改/新用户/行测统计）
  - 整数自增主键表：先把 C 新增行 id 平移到 B.max 之后，再并入 B 新增行
  - llm_models：并集 + 共同行以 B 覆盖，统一 timeout=600 / max_tokens=0
  - 聚合表清空，合并后由应用按统一口径重建
  - 清理孤儿 drills（保持此前的测试数据删除状态）

用法：python scripts/merge_server_20260909.py   （在项目根目录执行）
幂等性：每次从 C 重新拷贝 merged.db，可反复执行。
注意：线上库已完成合并切换；重跑会重新生成 merged.db，不影响已上线的
data/slb.db。
"""
import os
import shutil
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A_PATH = os.path.join(ROOT, "data", "slb.db.local-pre-20260904")
B_PATH = os.path.join(ROOT, "data", "merge-20260909", "server-20260909.db")
C_PATH = os.path.join(ROOT, "data", "slb.db")
OUT_PATH = os.path.join(ROOT, "data", "merge-20260909", "merged.db")

AGG_TABLES = [
    "user_question_type_stats", "diagnostic_reports", "daily_practice",
    "daily_practice_streaks", "user_dimension_trends", "user_weak_points",
]


def main():
    for p in (A_PATH, B_PATH, C_PATH):
        assert os.path.exists(p), f"缺少数据库文件: {p}"

    shutil.copyfile(C_PATH, OUT_PATH)
    m = sqlite3.connect(OUT_PATH)
    m.execute("PRAGMA foreign_keys = OFF")
    m.execute("ATTACH DATABASE ? AS adb", (A_PATH,))
    m.execute("ATTACH DATABASE ? AS bdb", (B_PATH,))

    # ================= TEXT/UUID 主键表：并入 B 新增 =================
    # users
    m.execute(
        "INSERT OR IGNORE INTO main.users "
        "SELECT * FROM bdb.users WHERE uid NOT IN (SELECT uid FROM adb.users)"
    )
    # papers
    m.execute(
        "INSERT OR IGNORE INTO main.papers "
        "SELECT * FROM bdb.papers WHERE pid NOT IN (SELECT pid FROM adb.papers)"
    )
    # submissions
    m.execute(
        "INSERT OR IGNORE INTO main.submissions "
        "SELECT * FROM bdb.submissions WHERE sid NOT IN (SELECT sid FROM adb.submissions)"
    )
    # user_points
    m.execute(
        "INSERT OR IGNORE INTO main.user_points "
        "SELECT * FROM bdb.user_points WHERE uid NOT IN (SELECT uid FROM adb.user_points)"
    )
    m.commit()
    print("[TEXT主键] users/papers/submissions/user_points 已并入 B 新增行")

    # ================= 整数自增主键表：平移 + 并入 =================
    # 每张表：先把 C 相对 A 的新增行 id 整体平移 B.max，再并入 B 新增行。

    bmax = m.execute("SELECT COALESCE(MAX(id),0) FROM bdb.question_type_drills").fetchone()[0]
    m.execute(
        "UPDATE main.question_type_drills SET id = id + ? "
        "WHERE id NOT IN (SELECT id FROM adb.question_type_drills)", (bmax,))
    m.execute(
        "INSERT OR IGNORE INTO main.question_type_drills SELECT * FROM bdb.question_type_drills "
        "WHERE id NOT IN (SELECT id FROM adb.question_type_drills)")

    bmax = m.execute("SELECT COALESCE(MAX(id),0) FROM bdb.admin_logs").fetchone()[0]
    m.execute(
        "UPDATE main.admin_logs SET id = id + ? "
        "WHERE id NOT IN (SELECT id FROM adb.admin_logs)", (bmax,))
    m.execute(
        "INSERT OR IGNORE INTO main.admin_logs SELECT * FROM bdb.admin_logs "
        "WHERE id NOT IN (SELECT id FROM adb.admin_logs)")

    bmax = m.execute("SELECT COALESCE(MAX(id),0) FROM bdb.learning_records").fetchone()[0]
    m.execute(
        "UPDATE main.learning_records SET id = id + ? "
        "WHERE id NOT IN (SELECT id FROM adb.learning_records)", (bmax,))
    m.execute(
        "INSERT OR IGNORE INTO main.learning_records SELECT * FROM bdb.learning_records "
        "WHERE id NOT IN (SELECT id FROM adb.learning_records)")

    bmax = m.execute("SELECT COALESCE(MAX(id),0) FROM bdb.credit_transactions").fetchone()[0]
    m.execute(
        "UPDATE main.credit_transactions SET id = id + ? "
        "WHERE id NOT IN (SELECT id FROM adb.credit_transactions)", (bmax,))
    m.execute(
        "INSERT OR IGNORE INTO main.credit_transactions SELECT * FROM bdb.credit_transactions "
        "WHERE id NOT IN (SELECT id FROM adb.credit_transactions)")

    bmax = m.execute("SELECT COALESCE(MAX(id),0) FROM bdb.token_usage_logs").fetchone()[0]
    m.execute(
        "UPDATE main.token_usage_logs SET id = id + ? "
        "WHERE id NOT IN (SELECT id FROM adb.token_usage_logs)", (bmax,))
    m.execute(
        "INSERT OR IGNORE INTO main.token_usage_logs SELECT * FROM bdb.token_usage_logs "
        "WHERE id NOT IN (SELECT id FROM adb.token_usage_logs)")

    bmax = m.execute("SELECT COALESCE(MAX(id),0) FROM bdb.weak_points").fetchone()[0]
    m.execute(
        "UPDATE main.weak_points SET id = id + ? "
        "WHERE id NOT IN (SELECT id FROM adb.weak_points)", (bmax,))
    m.execute(
        "INSERT OR IGNORE INTO main.weak_points SELECT * FROM bdb.weak_points "
        "WHERE id NOT IN (SELECT id FROM adb.weak_points)")

    bmax = m.execute("SELECT COALESCE(MAX(id),0) FROM bdb.sign_in_records").fetchone()[0]
    m.execute(
        "UPDATE main.sign_in_records SET id = id + ? "
        "WHERE id NOT IN (SELECT id FROM adb.sign_in_records)", (bmax,))
    m.execute(
        "INSERT OR IGNORE INTO main.sign_in_records SELECT * FROM bdb.sign_in_records "
        "WHERE id NOT IN (SELECT id FROM adb.sign_in_records)")

    bmax = m.execute("SELECT COALESCE(MAX(id),0) FROM bdb.hot_topics").fetchone()[0]
    m.execute(
        "UPDATE main.hot_topics SET id = id + ? "
        "WHERE id NOT IN (SELECT id FROM adb.hot_topics)", (bmax,))
    m.execute(
        "INSERT OR IGNORE INTO main.hot_topics SELECT * FROM bdb.hot_topics "
        "WHERE id NOT IN (SELECT id FROM adb.hot_topics)")

    bmax = m.execute("SELECT COALESCE(MAX(id),0) FROM bdb.user_topic_learning").fetchone()[0]
    m.execute(
        "UPDATE main.user_topic_learning SET id = id + ? "
        "WHERE id NOT IN (SELECT id FROM adb.user_topic_learning)", (bmax,))
    m.execute(
        "INSERT OR IGNORE INTO main.user_topic_learning SELECT * FROM bdb.user_topic_learning "
        "WHERE id NOT IN (SELECT id FROM adb.user_topic_learning)")

    bmax = m.execute("SELECT COALESCE(MAX(id),0) FROM bdb.community_posts").fetchone()[0]
    m.execute(
        "UPDATE main.community_posts SET id = id + ? "
        "WHERE id NOT IN (SELECT id FROM adb.community_posts)", (bmax,))
    m.execute(
        "INSERT OR IGNORE INTO main.community_posts SELECT * FROM bdb.community_posts "
        "WHERE id NOT IN (SELECT id FROM adb.community_posts)")

    bmax = m.execute("SELECT COALESCE(MAX(id),0) FROM bdb.community_comments").fetchone()[0]
    m.execute(
        "UPDATE main.community_comments SET id = id + ? "
        "WHERE id NOT IN (SELECT id FROM adb.community_comments)", (bmax,))
    m.execute(
        "INSERT OR IGNORE INTO main.community_comments SELECT * FROM bdb.community_comments "
        "WHERE id NOT IN (SELECT id FROM adb.community_comments)")

    bmax = m.execute("SELECT COALESCE(MAX(id),0) FROM bdb.exchange_codes").fetchone()[0]
    m.execute(
        "UPDATE main.exchange_codes SET id = id + ? "
        "WHERE id NOT IN (SELECT id FROM adb.exchange_codes)", (bmax,))
    m.execute(
        "INSERT OR IGNORE INTO main.exchange_codes SELECT * FROM bdb.exchange_codes "
        "WHERE id NOT IN (SELECT id FROM adb.exchange_codes)")

    bmax = m.execute("SELECT COALESCE(MAX(id),0) FROM bdb.code_redemptions").fetchone()[0]
    m.execute(
        "UPDATE main.code_redemptions SET id = id + ? "
        "WHERE id NOT IN (SELECT id FROM adb.code_redemptions)", (bmax,))
    m.execute(
        "INSERT OR IGNORE INTO main.code_redemptions SELECT * FROM bdb.code_redemptions "
        "WHERE id NOT IN (SELECT id FROM adb.code_redemptions)")

    bmax = m.execute("SELECT COALESCE(MAX(id),0) FROM bdb.submission_judgments").fetchone()[0]
    m.execute(
        "UPDATE main.submission_judgments SET judgment_id = 'm' || judgment_id "
        "WHERE judgment_id NOT IN (SELECT judgment_id FROM adb.submission_judgments)", (bmax,))
    m.execute(
        "UPDATE main.submission_judgments SET judgment_id = substr(judgment_id, 2) || 'm' "
        "WHERE judgment_id LIKE 'm%'", (bmax,))
    m.execute(
        "INSERT OR IGNORE INTO main.submission_judgments SELECT * FROM bdb.submission_judgments "
        "WHERE judgment_id NOT IN (SELECT judgment_id FROM adb.submission_judgments)")

    # tickets / ticket_replies / chat_messages / packages / user_packages：逐表平移+并入

    bmax = m.execute("SELECT COALESCE(MAX(id),0) FROM bdb.tickets").fetchone()[0]
    m.execute("UPDATE main.tickets SET id = id + ? WHERE id NOT IN (SELECT id FROM adb.tickets)", (bmax,))
    m.execute("INSERT OR IGNORE INTO main.tickets SELECT * FROM bdb.tickets WHERE id NOT IN (SELECT id FROM adb.tickets)")

    bmax = m.execute("SELECT COALESCE(MAX(id),0) FROM bdb.ticket_replies").fetchone()[0]
    m.execute("UPDATE main.ticket_replies SET id = id + ? WHERE id NOT IN (SELECT id FROM adb.ticket_replies)", (bmax,))
    m.execute("INSERT OR IGNORE INTO main.ticket_replies SELECT * FROM bdb.ticket_replies WHERE id NOT IN (SELECT id FROM adb.ticket_replies)")

    bmax = m.execute("SELECT COALESCE(MAX(id),0) FROM bdb.chat_messages").fetchone()[0]
    m.execute("UPDATE main.chat_messages SET id = id + ? WHERE id NOT IN (SELECT id FROM adb.chat_messages)", (bmax,))
    m.execute("INSERT OR IGNORE INTO main.chat_messages SELECT * FROM bdb.chat_messages WHERE id NOT IN (SELECT id FROM adb.chat_messages)")

    bmax = m.execute("SELECT COALESCE(MAX(id),0) FROM bdb.packages").fetchone()[0]
    m.execute("UPDATE main.packages SET id = id + ? WHERE id NOT IN (SELECT id FROM adb.packages)", (bmax,))
    m.execute("INSERT OR IGNORE INTO main.packages SELECT * FROM bdb.packages WHERE id NOT IN (SELECT id FROM adb.packages)")

    bmax = m.execute("SELECT COALESCE(MAX(id),0) FROM bdb.user_packages").fetchone()[0]
    m.execute("UPDATE main.user_packages SET id = id + ? WHERE id NOT IN (SELECT id FROM adb.user_packages)", (bmax,))
    m.execute("INSERT OR IGNORE INTO main.user_packages SELECT * FROM bdb.user_packages WHERE id NOT IN (SELECT id FROM adb.user_packages)")

    # essay_anchors：复合主键，uuid 文本，直接并入
    m.execute(
        "INSERT OR IGNORE INTO main.essay_anchors SELECT * FROM bdb.essay_anchors "
        "WHERE anchor_key NOT IN (SELECT anchor_key FROM main.essay_anchors)")

    # question_tags（服务器侧新增表；若本地无此表则按 B 结构建表后并入）
    has_tags = m.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='question_tags'"
    ).fetchone()[0]
    if not has_tags:
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
        "INSERT OR IGNORE INTO main.question_tags SELECT * FROM bdb.question_tags "
        "WHERE id NOT IN (SELECT id FROM main.question_tags)")

    m.commit()
    print("[整数主键] 各表已平移并并入 B 新增行")

    # ================= llm_models：并集 + 共同行按 B 覆盖 =================
    m.execute(
        "INSERT OR IGNORE INTO main.llm_models "
        "SELECT * FROM bdb.llm_models WHERE model_id NOT IN (SELECT model_id FROM adb.llm_models)"
    )
    m.execute(
        """UPDATE main.llm_models SET
             name = (SELECT name FROM bdb.llm_models WHERE bdb.llm_models.model_id = main.llm_models.model_id),
             protocol = (SELECT protocol FROM bdb.llm_models WHERE bdb.llm_models.model_id = main.llm_models.model_id),
             base_url = (SELECT base_url FROM bdb.llm_models WHERE bdb.llm_models.model_id = main.llm_models.model_id),
             model_name = (SELECT model_name FROM bdb.llm_models WHERE bdb.llm_models.model_id = main.llm_models.model_id),
             api_key_ciphertext = (SELECT api_key_ciphertext FROM bdb.llm_models WHERE bdb.llm_models.model_id = main.llm_models.model_id),
             weight = (SELECT weight FROM bdb.llm_models WHERE bdb.llm_models.model_id = main.llm_models.model_id),
             priority = (SELECT priority FROM bdb.llm_models WHERE bdb.llm_models.model_id = main.llm_models.model_id),
             enabled = (SELECT enabled FROM bdb.llm_models WHERE bdb.llm_models.model_id = main.llm_models.model_id),
             public_visible = (SELECT public_visible FROM bdb.llm_models WHERE bdb.llm_models.model_id = main.llm_models.model_id),
             credit_cost = (SELECT credit_cost FROM bdb.llm_models WHERE bdb.llm_models.model_id = main.llm_models.model_id),
             input_price_per_mtok = (SELECT input_price_per_mtok FROM bdb.llm_models WHERE bdb.llm_models.model_id = main.llm_models.model_id),
             output_price_per_mtok = (SELECT output_price_per_mtok FROM bdb.llm_models WHERE bdb.llm_models.model_id = main.llm_models.model_id)
           WHERE model_id IN (SELECT model_id FROM bdb.llm_models)"""
    )
    m.execute("UPDATE main.llm_models SET timeout_seconds = 600, max_tokens = 0")
    m.commit()
    total = m.execute("SELECT COUNT(*) FROM llm_models").fetchone()[0]
    print(f"[llm_models] 并集共 {total} 个，全部 timeout=600 / max_tokens=不限")

    # ================= 聚合表清空（合并后由应用重算）=================
    m.execute("DELETE FROM main.user_question_type_stats")
    m.execute("DELETE FROM main.diagnostic_reports")
    m.execute("DELETE FROM main.daily_practice")
    m.execute("DELETE FROM main.daily_practice_streaks")
    m.execute("DELETE FROM main.user_dimension_trends")
    m.execute("DELETE FROM main.user_weak_points")
    m.commit()
    print("[聚合清空] 题型统计/诊断报告/趋势/每日练习")

    # ================= 清理孤儿 drills（保持测试数据删除状态）=================
    m.execute(
        "DELETE FROM main.question_type_drills "
        "WHERE sid IS NOT NULL AND sid NOT IN (SELECT sid FROM main.submissions)"
    )
    m.execute(
        "DELETE FROM main.submission_judgments "
        "WHERE sid NOT IN (SELECT sid FROM main.submissions)"
    )
    m.commit()
    print("[污染清理] 孤儿 drills / 孤儿判定已删除")

    m.execute("VACUUM")
    m.commit()

    print("\n=== 合并结果速览 ===")
    for t in ["users", "submissions", "question_type_drills", "submission_judgments", "llm_models"]:
        cnt = m.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
        print(f"  {t:24} merged={cnt}")

    m.close()
    print(f"\n完成 -> {OUT_PATH}")


if __name__ == "__main__":
    main()
