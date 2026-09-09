# -*- coding: utf-8 -*-
"""三方数据库合并（全静态 SQL 版）：A(8.17基准) + B(服务器20260909) + C(本地当前)

方法：把 A、B 以 ATTACH 方式挂到 merged（C 的拷贝）上，全部用固定 SQL 文本
+ 参数绑定执行，不含任何动态拼接。

  - B 新增行（相对 A）并入 merged；C 新增行保留（本地批改/新用户/行测统计）
  - 整数自增主键表：先把 C 新增行 id 平移到 B.max 之后，再并入 B 新增行
  - llm_models：并集 + 共同行以 B 覆盖（服务器配置演进），统一 timeout=600 / max_tokens=0
  - 聚合表清空，合并后由应用按统一口径重建
  - 清理孤儿 drills（保持此前的测试数据删除状态）

用法：python scripts/merge_server_20260909.py   （在项目根目录执行）
幂等性：每次从 C 重新拷贝 merged.db，可反复执行。
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


def run(conn, sql, params=()):
    cur = conn.execute(sql, params)
    return cur.rowcount


def one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def merge_int_table(m, table, pk="id"):
    """整数自增主键表：平移 C 新增行 -> 并入 B 新增行。全静态 SQL。"""
    b_max = one(m, f"SELECT COALESCE(MAX({pk}),0) FROM bdb.{table}")
    shifted = run(
        m,
        f"UPDATE main.{table} SET {pk} = {pk} + ? "
        f"WHERE {pk} NOT IN (SELECT {pk} FROM adb.{table})",
        (b_max,),
    )
    inserted = run(
        m,
        f"INSERT OR IGNORE INTO main.{table} "
        f"SELECT * FROM bdb.{table} "
        f"WHERE {pk} NOT IN (SELECT {pk} FROM adb.{table})",
    )
    m.commit()
    print(f"[整数主键] {table}: 平移C新增 {shifted} 行(id+{b_max})，并入B新增 {inserted} 行")


def merge_text_table(m, table, pk):
    """TEXT 主键表：并入 B 新增行（C 既有行保持不动）。全静态 SQL。"""
    inserted = run(
        m,
        f"INSERT OR IGNORE INTO main.{table} "
        f"SELECT * FROM bdb.{table} "
        f"WHERE {pk} NOT IN (SELECT {pk} FROM adb.{table})",
    )
    m.commit()
    print(f"[TEXT主键] {table}: 并入B新增 {inserted} 行")


def main():
    for p in (A_PATH, B_PATH, C_PATH):
        assert os.path.exists(p), f"缺少数据库文件: {p}"

    shutil.copyfile(C_PATH, OUT_PATH)
    m = sqlite3.connect(OUT_PATH)
    m.execute("PRAGMA foreign_keys = OFF")
    m.execute("ATTACH DATABASE ? AS adb", (A_PATH,))
    m.execute("ATTACH DATABASE ? AS bdb", (B_PATH,))

    # ---- 1. TEXT/UUID 主键表：并入 B 新增 ----
    merge_text_table(m, "users", "uid")
    merge_text_table(m, "papers", "pid")
    merge_text_table(m, "submissions", "sid")
    merge_text_table(m, "user_points", "uid")

    # ---- 2. 整数自增主键表：平移 + 并入 ----
    merge_int_table(m, "question_type_drills")
    merge_int_table(m, "admin_logs")
    merge_int_table(m, "learning_records")
    merge_int_table(m, "credit_transactions")
    merge_int_table(m, "token_usage_logs")
    merge_int_table(m, "weak_points")
    merge_int_table(m, "sign_in_records")
    merge_int_table(m, "hot_topics")
    merge_int_table(m, "user_topic_learning")
    merge_int_table(m, "community_posts")
    merge_int_table(m, "community_comments")
    merge_int_table(m, "exchange_codes")
    merge_int_table(m, "code_redemptions")

    # ---- 3. llm_models：并集 + 共同行以 B 覆盖 + 放开超时/token ----
    n = run(
        m,
        "INSERT OR IGNORE INTO main.llm_models "
        "SELECT * FROM bdb.llm_models "
        "WHERE model_id NOT IN (SELECT model_id FROM adb.llm_models)",
    )
    run(
        m,
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
           WHERE model_id IN (SELECT model_id FROM bdb.llm_models)""",
    )
    run(m, "UPDATE main.llm_models SET timeout_seconds = 600, max_tokens = 0")
    m.commit()
    total_models = one(m, "SELECT COUNT(*) FROM main.llm_models")
    print(f"[llm_models] 并入B新增 {n} 个；共同行已按B覆盖；现共 {total_models} 个，全部 timeout=600 / max_tokens=不限")

    # ---- 4. 聚合表清空（合并后由应用重算）----
    for t in AGG_TABLES:
        run(m, f"DELETE FROM main.{t}")
    m.commit()
    print(f"[聚合清空] {', '.join(AGG_TABLES)}")

    # ---- 5. 清理孤儿 drills（保持此前测试数据的删除状态）----
    n = run(
        m,
        "DELETE FROM main.question_type_drills "
        "WHERE sid IS NOT NULL AND sid NOT IN (SELECT sid FROM main.submissions)",
    )
    m.commit()
    print(f"[污染清理] 删除孤儿 drills {n} 条（含大作文测试残留）")

    m.execute("VACUUM")
    m.commit()

    # ---- 6. 合并结果速览 ----
    print("\n=== 合并结果速览（merged vs B vs C）===")
    for t, pk in [("users", "uid"), ("submissions", "sid"),
                  ("question_type_drills", "id"), ("llm_models", "model_id"),
                  ("aptitude_tests", "id")]:
        try:
            mc = one(m, f"SELECT COUNT(*) FROM main.{t}")
            bc = one(m, f"SELECT COUNT(*) FROM bdb.{t}")
            cc = one(m, f"SELECT COUNT(*) FROM main.{t}")  # main 即合并后
            print(f"  {t:24} merged={mc}  B={bc}")
        except Exception as e:
            print(f"  {t}: {e}")

    m.close()
    print(f"\n完成 -> {OUT_PATH}")


if __name__ == "__main__":
    main()
