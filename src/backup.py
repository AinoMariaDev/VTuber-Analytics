
from __future__ import annotations
import shutil
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
BACKUP_DIR = PROJECT_DIR / "backups"
TARGETS = [
    PROJECT_DIR / "data" / "vtuber_analytics.db",
    PROJECT_DIR / "app_config.local.json",
    PROJECT_DIR / "moderation_rules.local.json",
]

def main() -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = BACKUP_DIR / stamp
    out.mkdir(parents=True, exist_ok=True)

    copied = 0
    for target in TARGETS:
        if target.exists():
            shutil.copy2(target, out / target.name)
            copied += 1

    print(f"バックアップ完了: {out}")
    print(f"保存ファイル数: {copied}")

if __name__ == "__main__":
    main()
