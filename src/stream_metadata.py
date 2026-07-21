from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from storage_paths import DB_PATH, PROJECT_DIR

RULES_PATH = PROJECT_DIR / "stream_tag_rules.json"
WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]

DEFAULT_RULES: list[dict[str, Any]] = [
    {
        "tag": "友人戦",
        "keywords": [
            "友人戦", "視聴者参加型", "参加型", "参加歓迎", "誰でもok",
            "初見さん歓迎", "初見歓迎", "卓を囲", "一緒に麻雀"
        ],
        "requires_any": ["雀魂", "麻雀", "三麻", "四麻", "mahjongsoul"],
    },
    {
        "tag": "段位戦",
        "keywords": [
            "段位戦", "昇段戦", "昇段", "雀傑", "雀士", "雀豪",
            "ランク戦", "ランクマ"
        ],
        "requires_any": [],
    },
    {
        "tag": "雑談",
        "keywords": [
            "雑談", "お話", "語ろう", "質問", "マシュマロ", "作業配信",
            "本音", "相談", "トーク"
        ],
        "requires_any": [],
    },
    {
        "tag": "歌枠",
        "keywords": ["歌枠", "歌う", "熱唱", "カラオケ", "歌リクエスト"],
        "requires_any": [],
    },
    {
        "tag": "耐久",
        "keywords": ["耐久", "終われません", "終われない", "出るまで", "達成まで"],
        "requires_any": [],
    },
    {
        "tag": "企画配信",
        "keywords": ["初企画", "記念", "ビンゴ", "チャレンジ", "大会", "コラボ"],
        "requires_any": [],
    },
    {
        "tag": "ポケモン",
        "keywords": ["ポケモン", "pokemon", "チャンピオンズ"],
        "requires_any": [],
    },
    {
        "tag": "プライベートバトル",
        "keywords": ["プライベートバトル", "対戦歓迎", "対戦参加"],
        "requires_any": ["ポケモン", "pokemon", "チャンピオンズ"],
    },
    {
        "tag": "ゲーム",
        "keywords": ["龍が如く", "四ツ目神", "ホラゲー", "ゲーム実況"],
        "requires_any": [],
    },
]


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def ensure_stream_metadata_schema() -> None:
    with connect() as connection:
        existing = column_names(connection, "streams")
        additions = {
            "category": "TEXT",
            "weekday": "TEXT",
            "source_type": "TEXT",
            "last_synced_at": "TEXT",
        }
        for name, sql_type in additions.items():
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE streams ADD COLUMN {name} {sql_type}"
                )

        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS stream_tags (
                video_id TEXT NOT NULL,
                tag TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'auto',
                confidence REAL NOT NULL DEFAULT 1.0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (video_id, tag),
                FOREIGN KEY (video_id) REFERENCES streams(video_id)
            );

            CREATE INDEX IF NOT EXISTS idx_stream_tags_tag
            ON stream_tags(tag);

            CREATE TABLE IF NOT EXISTS stream_metadata_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        connection.execute(
            """
            UPDATE streams
            SET source_type = CASE
                WHEN source_type IS NOT NULL AND TRIM(source_type) <> ''
                    THEN source_type
                WHEN source_file LIKE '%.live_chat.json'
                    THEN 'yt-dlp'
                ELSE 'manual'
            END
            """
        )
        connection.execute(
            """
            UPDATE streams
            SET last_synced_at = COALESCE(last_synced_at, imported_at)
            """
        )
        connection.commit()


def load_rules() -> list[dict[str, Any]]:
    if not RULES_PATH.exists():
        RULES_PATH.write_text(
            json.dumps(
                {
                    "version": 1,
                    "description": "配信タイトルから複数企画タグを判定するルール",
                    "rules": DEFAULT_RULES,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return DEFAULT_RULES

    try:
        data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        rules = data.get("rules", []) if isinstance(data, dict) else []
        if isinstance(rules, list) and rules:
            return rules
    except (OSError, json.JSONDecodeError):
        pass
    return DEFAULT_RULES


def classify_tags(title: str, rules: list[dict[str, Any]]) -> list[str]:
    text = (title or "").casefold()
    tags: list[str] = []

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        tag = str(rule.get("tag", "")).strip()
        keywords = rule.get("keywords", [])
        requires_any = rule.get("requires_any", [])
        if not tag or not isinstance(keywords, list):
            continue

        matched = any(
            isinstance(keyword, str)
            and keyword.strip()
            and keyword.casefold() in text
            for keyword in keywords
        )
        if not matched:
            continue

        if isinstance(requires_any, list) and requires_any:
            requirement_met = any(
                isinstance(keyword, str)
                and keyword.strip()
                and keyword.casefold() in text
                for keyword in requires_any
            )
            if not requirement_met:
                continue

        if tag not in tags:
            tags.append(tag)

    if not tags:
        tags.append("その他")
    return tags


def weekday_from_date(stream_date: str | None) -> str:
    if not stream_date:
        return ""
    try:
        return WEEKDAYS[datetime.strptime(stream_date, "%Y-%m-%d").weekday()]
    except ValueError:
        return ""


def reclassify_all_streams(
    *,
    recompute_weekday: bool = True,
) -> dict[str, Any]:
    ensure_stream_metadata_schema()
    rules = load_rules()
    started_at = datetime.now().isoformat(timespec="seconds")
    changed_streams = 0
    total_tags = 0
    weekday_updates = 0
    tag_counts: dict[str, int] = {}

    with connect() as connection:
        rows = connection.execute(
            """
            SELECT video_id, stream_date, title, category, weekday
            FROM streams
            ORDER BY stream_date, video_id
            """
        ).fetchall()

        for row in rows:
            video_id = str(row["video_id"])
            tags = classify_tags(str(row["title"] or ""), rules)
            primary_category = tags[0]
            weekday = (
                weekday_from_date(row["stream_date"])
                if recompute_weekday
                else str(row["weekday"] or "")
            )

            old_tags = {
                str(item["tag"])
                for item in connection.execute(
                    "SELECT tag FROM stream_tags WHERE video_id = ?",
                    (video_id,),
                ).fetchall()
            }
            if old_tags != set(tags) or str(row["category"] or "") != primary_category:
                changed_streams += 1

            if recompute_weekday and str(row["weekday"] or "") != weekday:
                weekday_updates += 1

            connection.execute(
                "DELETE FROM stream_tags WHERE video_id = ? AND source = 'auto'",
                (video_id,),
            )
            for tag in tags:
                connection.execute(
                    """
                    INSERT INTO stream_tags(
                        video_id, tag, source, confidence, updated_at
                    )
                    VALUES (?, ?, 'auto', 1.0, CURRENT_TIMESTAMP)
                    ON CONFLICT(video_id, tag) DO UPDATE SET
                        source = CASE
                            WHEN stream_tags.source = 'manual'
                                THEN stream_tags.source
                            ELSE excluded.source
                        END,
                        confidence = CASE
                            WHEN stream_tags.source = 'manual'
                                THEN stream_tags.confidence
                            ELSE excluded.confidence
                        END,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (video_id, tag),
                )
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
                total_tags += 1

            connection.execute(
                """
                UPDATE streams
                SET category = ?,
                    weekday = ?,
                    last_synced_at = CURRENT_TIMESTAMP
                WHERE video_id = ?
                """,
                (primary_category, weekday, video_id),
            )

        details = {
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "stream_count": len(rows),
            "changed_streams": changed_streams,
            "weekday_updates": weekday_updates,
            "total_tags": total_tags,
            "tag_counts": tag_counts,
            "rule_count": len(rules),
        }
        connection.execute(
            """
            INSERT INTO stream_metadata_history(action, details_json)
            VALUES ('reclassify_all', ?)
            """,
            (json.dumps(details, ensure_ascii=False),),
        )
        connection.commit()

    return details


def metadata_status() -> dict[str, Any]:
    ensure_stream_metadata_schema()
    with connect() as connection:
        stream_count = int(
            connection.execute("SELECT COUNT(*) FROM streams").fetchone()[0]
        )
        tagged_stream_count = int(
            connection.execute(
                "SELECT COUNT(DISTINCT video_id) FROM stream_tags"
            ).fetchone()[0]
        )
        tag_count = int(
            connection.execute("SELECT COUNT(*) FROM stream_tags").fetchone()[0]
        )
        missing_weekday = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM streams
                WHERE weekday IS NULL OR TRIM(weekday) = ''
                """
            ).fetchone()[0]
        )
        source_counts = {
            str(row["source_type"] or "不明"): int(row["count"])
            for row in connection.execute(
                """
                SELECT source_type, COUNT(*) AS count
                FROM streams
                GROUP BY source_type
                """
            ).fetchall()
        }
        latest = connection.execute(
            """
            SELECT details_json, created_at
            FROM stream_metadata_history
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    latest_result: dict[str, Any] | None = None
    if latest:
        try:
            latest_result = json.loads(str(latest["details_json"]))
        except json.JSONDecodeError:
            latest_result = {"raw": str(latest["details_json"])}
        latest_result["recorded_at"] = latest["created_at"]

    return {
        "stream_count": stream_count,
        "tagged_stream_count": tagged_stream_count,
        "tag_count": tag_count,
        "missing_weekday": missing_weekday,
        "source_counts": source_counts,
        "latest_result": latest_result,
        "rules_path": str(RULES_PATH),
        "youtube_ready": True,
        "identity_key": "video_id",
    }


def category_analysis(
    owner_channel_ids: list[str],
) -> list[dict[str, Any]]:
    ensure_stream_metadata_schema()
    owners = [value for value in owner_channel_ids if value]
    owner_clause = ""
    params: list[Any] = []
    if owners:
        placeholders = ",".join("?" for _ in owners)
        owner_clause = f"WHERE channel_id NOT IN ({placeholders})"
        params.extend(owners)

    with connect() as connection:
        rows = connection.execute(
            f"""
            WITH message_totals AS (
                SELECT
                    video_id,
                    COUNT(DISTINCT channel_id) AS participants,
                    COUNT(message_id) AS comments
                FROM messages
                {owner_clause}
                GROUP BY video_id
            )
            SELECT
                st.tag AS category,
                COUNT(DISTINCT st.video_id) AS stream_count,
                ROUND(AVG(COALESCE(mt.participants, 0)), 1) AS avg_participants,
                ROUND(AVG(COALESCE(mt.comments, 0)), 1) AS avg_comments,
                MAX(COALESCE(mt.participants, 0)) AS max_participants,
                MAX(COALESCE(mt.comments, 0)) AS max_comments
            FROM stream_tags st
            JOIN streams s ON s.video_id = st.video_id
            LEFT JOIN message_totals mt ON mt.video_id = st.video_id
            GROUP BY st.tag
            ORDER BY avg_participants DESC, avg_comments DESC, st.tag
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def stream_tag_map() -> dict[str, list[str]]:
    ensure_stream_metadata_schema()
    result: dict[str, list[str]] = {}
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT video_id, tag
            FROM stream_tags
            ORDER BY video_id, tag
            """
        ).fetchall()
    for row in rows:
        result.setdefault(str(row["video_id"]), []).append(str(row["tag"]))
    return result
