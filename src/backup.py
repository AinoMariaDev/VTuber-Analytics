
from __future__ import annotations
import shutil
from datetime import datetime
from pathlib import Path

from storage_paths import BACKUP_DIR, CONFIG_PATH, DB_PATH, MODERATION_RULES_PATH

TARGETS = [DB_PATH, CONFIG_PATH, MODERATION_RULES_PATH]

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
