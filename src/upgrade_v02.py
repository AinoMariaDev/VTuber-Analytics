from __future__ import annotations
from common import connect
from schema import initialize

COLUMNS = {
    "category": "TEXT",
    "weekday": "TEXT",
    "duration_minutes": "INTEGER",
    "notes": "TEXT"
}

def column_names(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

def main() -> None:
    initialize()
    with connect() as conn:
        existing = column_names(conn, "streams")
        for name, sql_type in COLUMNS.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE streams ADD COLUMN {name} {sql_type}")
                print(f"[追加] streams.{name}")
        conn.commit()
    print("v0.2用データベース更新が完了しました。")

if __name__ == "__main__":
    main()
