from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from common import PROJECT_DIR, connect

CATEGORY_CONFIG = PROJECT_DIR / "config_categories.json"
WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]


def load_category_config() -> tuple[list[dict[str, Any]], str]:
    with CATEGORY_CONFIG.open("r", encoding="utf-8") as file:
        config = json.load(file)

    if isinstance(config, dict) and isinstance(config.get("rules"), list):
        rules = config["rules"]
        default_category = str(
            config.get("default_category", "その他")
        ).strip() or "その他"
        return rules, default_category

    if isinstance(config, dict):
        rules: list[dict[str, Any]] = []
        for category, value in config.items():
            if category == "default_category":
                continue

            if isinstance(value, dict):
                keywords = value.get("keywords", [])
            else:
                keywords = value

            if not isinstance(keywords, list):
                keywords = []

            rules.append({
                "category": str(category),
                "keywords": keywords,
            })

        default_category = str(
            config.get("default_category", "その他")
        ).strip() or "その他"
        return rules, default_category

    raise ValueError("config_categories.json の形式が正しくありません。")


def classify(
    title: str,
    rules: list[dict[str, Any]],
    default_category: str,
) -> str:
    lower_title = (title or "").lower()

    for rule in rules:
        if not isinstance(rule, dict):
            continue

        category = str(rule.get("category", "")).strip()
        if not category or category == default_category:
            continue

        keywords = rule.get("keywords", [])
        if not isinstance(keywords, list):
            continue

        for keyword in keywords:
            if isinstance(keyword, str) and keyword.strip():
                if keyword.lower() in lower_title:
                    return category

    return default_category


def main() -> None:
    rules, default_category = load_category_config()

    updated = 0
    with connect() as conn:
        rows = conn.execute(
            "SELECT video_id, stream_date, title FROM streams"
        ).fetchall()

        for row in rows:
            category = classify(
                row["title"],
                rules,
                default_category,
            )

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

        # 古い誤分類が残っていた場合も強制的に統合
        conn.execute(
            """
            UPDATE streams
            SET category = ?
            WHERE category = 'default_category'
               OR category IS NULL
               OR TRIM(category) = ''
            """,
            (default_category,),
        )

        conn.commit()

    print(f"{updated}配信を企画分類しました。")


if __name__ == "__main__":
    main()
