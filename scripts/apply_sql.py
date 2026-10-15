"""SQLファイルをSupabaseに適用する
使い方: python scripts/apply_sql.py sql/001_init.sql
"""

import sys

from db import connect

sql_path = sys.argv[1]
with open(sql_path) as f:
    sql = f.read()

with connect() as conn:
    with conn.cursor() as cur:
        cur.execute(sql)

print(f"OK: {sql_path} を適用しました")
