from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from common import PROJECT_DIR, connect

CATEGORY_CONFIG = PROJECT_DIR / "config_categories.json"

WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]

def classify(title: str, rules: dict[str, list[str]]) -> str:
    lower = title.lower()
    for category, keywords in rules.items():
        if category == "その他":
            continue
        for keyword in keywords:
            if keyword.lower() in lower:
                return category
    return "その他"

def main() -> None:
    with CATEGORY_CONFIG.open("r", encoding="utf-8") as f:
        rules = json.load(f)

    updated = 0
    with connect() as conn:
        rows = conn.execute(
            "SELECT video_id, stream_date, title FROM streams"
        ).fetchall()

        for row in rows:
            category = classify(row["title"], rules)
            weekday = ""
            if row["stream_date"]:
                dt = datetime.strptime(row["stream_date"], "%Y-%m-%d")
                weekday = WEEKDAYS[dt.weekday()]

            conn.execute(
                """
                UPDATE streams
                SET category = ?, weekday = ?
                WHERE video_id = ?
                """,
                (category, weekday, row["video_id"]),
            )
            updated += 1

        conn.commit()

    print(f"{updated}配信を企画分類しました。")

if __name__ == "__main__":
    main()
