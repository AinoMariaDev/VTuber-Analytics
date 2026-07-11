from __future__ import annotations
from common import DB_PATH, connect

def main() -> None:
    if not DB_PATH.exists():
        print("データベースがまだありません。")
        print("先に 4_データベース構築.bat を実行してください。")
        return

    with connect() as conn:
        streams = conn.execute("SELECT COUNT(*) FROM streams").fetchone()[0]
        listeners = conn.execute("SELECT COUNT(*) FROM listeners").fetchone()[0]
        messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        latest = conn.execute("SELECT MAX(stream_date) FROM streams").fetchone()[0]

    print("VTuber Analytics データベース状況")
    print("--------------------------------")
    print(f"配信数: {streams}")
    print(f"リスナー数: {listeners}")
    print(f"コメント数: {messages}")
    print(f"最新配信日: {latest}")
    print(f"DB: {DB_PATH}")

if __name__ == "__main__":
    main()
