from __future__ import annotations

import json
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "app_config.local.json"


def ask_required(label: str, example: str = "") -> str:
    while True:
        value = input(f"{label}: ").strip()
        if value:
            return value
        print("入力が必要です。")
        if example:
            print(f"入力例: {example}")


def ask_optional(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def main() -> None:
    print("VTuber Analytics 初期設定")
    print("--------------------------------")
    print("このアプリは Aino Maria が開発・公開しています。")
    print("アプリ名と Powered by 表記は固定です。")
    print()

    print("【チャンネル名】")
    print("分析対象にするご自身のYouTubeチャンネル名を入力してください。")
    print("入力例: 愛野まりあ")
    channel_name = ask_required("チャンネル名", "愛野まりあ")
    print()

    print("【本人のYouTubeチャンネルID】")
    print("「UC」から始まるYouTube固有IDを入力してください。")
    print("本人のコメントをリスナー集計から除外するために使用します。")
    print("「@○○」のハンドル名やチャンネルURLではありません。")
    print()
    print("確認場所:")
    print("YouTube → 右上のプロフィール画像 → 設定 → 詳細設定 → チャンネルID")
    print()
    print("入力例: UCbPtcsXkPLLiOySGZJW92gw")
    owner_channel_id = ask_required(
        "本人のYouTubeチャンネルID",
        "UCbPtcsXkPLLiOySGZJW92gw",
    )
    print()

    default_chat_dir = str(PROJECT_DIR / "youtube_chat_data")
    print("【ライブチャットJSONの保存フォルダ】")
    print("通常は変更不要です。Enterを押すと既定の保存先を使用します。")
    chat_data_dir = ask_optional(
        "保存フォルダ",
        default_chat_dir,
    )

    config = {
        "app_name": "VTuber Analytics",
        "powered_by": "Aino Maria",
        "channel_name": channel_name,
        "owner_channel_ids": [owner_channel_id],
        "chat_data_dir": chat_data_dir,
        "theme_color": "#24324A",
        "host": "127.0.0.1",
        "port": 8765,
    }

    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    Path(chat_data_dir).expanduser().mkdir(parents=True, exist_ok=True)

    print()
    print("--------------------------------")
    print("初期設定が完了しました。")
    print(f"チャンネル名: {channel_name}")
    print(f"チャット保存先: {chat_data_dir}")
    print()
    print("次は 2_チャット取得.bat を実行してください。")
    print("app_config.local.json はGitHubへアップロードしないでください。")


if __name__ == "__main__":
    main()
