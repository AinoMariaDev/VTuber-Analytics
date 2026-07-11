
from __future__ import annotations
import json
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "app_config.local.json"

def ask(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default

def main() -> None:
    print("VTuber Analytics 初期設定")
    print("--------------------------------")
    channel_name = ask("チャンネル名", "Aino Maria")
    powered_by = ask("Powered by 表記", "Aino Maria")
    owner_channel_id = ask(
        "配信者本人のYouTubeチャンネルID",
        "UCbPtcsXkPLLiOySGZJW92gw",
    )

    config = {
        "app_name": "VTuber Analytics",
        "powered_by": powered_by,
        "channel_name": channel_name,
        "owner_channel_ids": [owner_channel_id],
        "host": "127.0.0.1",
        "port": 8765,
    }
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print()
    print(f"保存しました: {CONFIG_PATH}")
    print("このファイルはGitHubへアップロードしないでください。")

if __name__ == "__main__":
    main()
