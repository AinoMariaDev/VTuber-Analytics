from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent


def run(script: str, description: str) -> bool:
    path = PROJECT_DIR / "src" / script
    print()
    print(f"【{description}】")
    result = subprocess.run([sys.executable, str(path)], cwd=PROJECT_DIR)
    return result.returncode == 0


def main() -> None:
    print("VTuber Analytics データ更新")
    print("--------------------------------")
    print("「2_チャット取得.bat」をすでに実行している場合は、")
    print("そのまま Enter を押してください。")
    print()
    print("まだ実行していない場合は「y」を入力すると、")
    print("このままYouTubeチャットを取得します。")
    print()

    answer = input("今からYouTubeチャットも取得しますか？ [y/N]: ").strip().lower()
    if answer == "y":
        if not run("download_chats.py", "YouTubeチャットを取得しています"):
            print()
            print("チャット取得に失敗したため、データ更新を中止しました。")
            raise SystemExit(1)

    steps = [
        ("upgrade_v02.py", "データベースを更新しています"),
        ("classify_streams.py", "配信カテゴリを分類しています"),
        ("build_database.py", "チャットデータを読み込んでいます"),
        ("generate_reports.py", "分析レポートを作成しています"),
    ]

    for script, description in steps:
        path = PROJECT_DIR / "src" / script
        if path.exists() and not run(script, description):
            print()
            print(f"{description} の途中でエラーが発生しました。")
            print("表示されたエラー内容を確認してください。")
            raise SystemExit(1)

    print()
    print("--------------------------------")
    print("データ更新が完了しました。")
    print()
    print("次は 4_Webアプリ起動.bat を実行してください。")
    print("--------------------------------")


if __name__ == "__main__":
    main()
