
from __future__ import annotations
import json
import sqlite3
import sys
from pathlib import Path

from storage_paths import CONFIG_PATH, DB_PATH, PROJECT_DIR

WEB_PATH = PROJECT_DIR / "web" / "index.html"

def main() -> None:
    errors = []
    warnings = []

    if not DB_PATH.exists():
        errors.append("データベースがありません。")
    else:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                stream_count = conn.execute("SELECT COUNT(*) FROM streams").fetchone()[0]
                listener_count = conn.execute("SELECT COUNT(*) FROM listeners").fetchone()[0]
                message_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            print(f"配信数: {stream_count}")
            print(f"リスナー数: {listener_count}")
            print(f"コメント数: {message_count}")
        except Exception as exc:
            errors.append(f"データベース確認失敗: {exc}")

    if not WEB_PATH.exists():
        errors.append("web/index.html がありません。")
    if not CONFIG_PATH.exists():
        warnings.append("初期設定ファイルがありません。初回起動時に自動作成されます。")

    print()
    if warnings:
        print("警告:")
        for w in warnings:
            print(f"- {w}")
    if errors:
        print("エラー:")
        for e in errors:
            print(f"- {e}")
        sys.exit(1)

    print("診断結果: 正常です。")

if __name__ == "__main__":
    main()
