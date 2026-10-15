"""Supabase への接続と pgvector セットアップを確認する"""

from db import connect

with connect() as conn:
    with conn.cursor() as cur:
        cur.execute("select extname from pg_extension where extname = 'vector'")
        if cur.fetchone() is None:
            print("NG: vector 拡張が有効化されていません")
        else:
            print("OK: vector 拡張は有効です")

        cur.execute(
            "select table_name from information_schema.tables "
            "where table_schema = 'public' and table_name in ('documents', 'chunks')"
        )
        found = {row[0] for row in cur.fetchall()}
        for table in ("documents", "chunks"):
            status = "OK" if table in found else "NG"
            print(f"{status}: {table} テーブル")
