from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import urllib.parse
from collections import defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_DIR / "data" / "vtuber_analytics.db"
INDEX_PATH = PROJECT_DIR / "web" / "index.html"
HOST = "127.0.0.1"
PORT = 8765

APP_CONFIG_PATH = PROJECT_DIR / "app_config.local.json"
APP_CONFIG_EXAMPLE_PATH = PROJECT_DIR / "app_config.example.json"

DEFAULT_APP_CONFIG = {
    "app_name": "VTuber Analytics",
    "powered_by": "Aino Maria",
    "channel_name": "",
"owner_channel_ids": ["YOUR_YOUTUBE_CHANNEL_ID"],
    "chat_data_dir": str(PROJECT_DIR / "youtube_chat_data"),
    "theme_color": "#24324A",
    "host": "127.0.0.1",
    "port": 8765
}

def load_app_config() -> dict[str, Any]:
    if not APP_CONFIG_PATH.exists():
        APP_CONFIG_PATH.write_text(
            json.dumps(DEFAULT_APP_CONFIG, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    try:
        with APP_CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = DEFAULT_APP_CONFIG.copy()
    return data


def configured_owner_channel_ids() -> set[str]:
    config = load_app_config()
    values = config.get("owner_channel_ids", [])
    result = {str(value).strip() for value in values if str(value).strip()}
    return result or {"YOUR_YOUTUBE_CHANNEL_ID"}


OWNER_CHANNEL_IDS = configured_owner_channel_ids()


MODERATION_RULES_PATH = PROJECT_DIR / "moderation_rules.local.json"
MODERATION_RULES_EXAMPLE_PATH = PROJECT_DIR / "moderation_rules.example.json"

DEFAULT_MODERATION_RULES = {
    "categories": {
        "sexual": {
            "label": "セクハラ・性的表現",
            "keywords": [
                "胸見せ", "パンツ見せ", "脱いで", "抱かせて",
                "キスして", "エロい", "下着", "おっぱい"
            ],
            "patterns": []
        },
        "distance": {
            "label": "距離感・独占的発言",
            "keywords": [
                "俺だけ見て", "他の男", "彼氏いる", "付き合って",
                "結婚して", "俺のもの"
            ],
            "patterns": []
        },
        "abuse": {
            "label": "暴言・攻撃的表現",
            "keywords": [
                "死ね", "消えろ", "気持ち悪い", "きもい"
            ],
            "patterns": []
        }
    }
}

def ensure_moderation_storage() -> None:
    if not MODERATION_RULES_PATH.exists():
        MODERATION_RULES_PATH.write_text(
            json.dumps(DEFAULT_MODERATION_RULES, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    with connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS moderation_reviews (
            message_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT '未確認',
            category TEXT,
            memo TEXT,
            reviewed_at TEXT,
            FOREIGN KEY (message_id) REFERENCES messages(message_id)
        );

        CREATE INDEX IF NOT EXISTS idx_moderation_status
        ON moderation_reviews(status);
        """)
        conn.commit()

def load_moderation_rules() -> dict[str, Any]:
    ensure_moderation_storage()
    try:
        with MODERATION_RULES_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return DEFAULT_MODERATION_RULES

def detect_rule_matches(text: str, rules: dict[str, Any]) -> list[dict[str, str]]:
    normalized = text.casefold()
    matches: list[dict[str, str]] = []
    for key, config in rules.get("categories", {}).items():
        label = str(config.get("label", key))
        for keyword in config.get("keywords", []):
            if str(keyword).casefold() in normalized:
                matches.append({
                    "category": key,
                    "label": label,
                    "rule": f"keyword:{keyword}",
                })
                break
        for pattern in config.get("patterns", []):
            try:
                if re.search(str(pattern), text, re.IGNORECASE):
                    matches.append({
                        "category": key,
                        "label": label,
                        "rule": f"regex:{pattern}",
                    })
                    break
            except re.error:
                continue
    return matches


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def placeholders(values: set[str]) -> str:
    return ",".join("?" for _ in values) or "''"

def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")

def latest_stream_date(conn: sqlite3.Connection) -> str | None:
    return conn.execute("SELECT MAX(stream_date) AS d FROM streams").fetchone()["d"]

def calculate_fan_score(
    participation_rate: float,
    avg_comments: float,
    days_absent: int | None,
    active_months: int,
) -> int:
    recency = 20
    if days_absent is None:
        recency = 0
    elif days_absent <= 14:
        recency = 20
    elif days_absent <= 30:
        recency = 14
    elif days_absent <= 60:
        recency = 7
    else:
        recency = 0

    score = (
        min(participation_rate / 0.7, 1) * 40
        + min(avg_comments / 35, 1) * 20
        + min(active_months / 8, 1) * 20
        + recency
    )
    return max(0, min(100, round(score)))

def classify_status(
    first_seen: str | None,
    last_seen: str | None,
    latest_date: str | None,
    recent_stream_count: int,
    previous_stream_count: int,
) -> str:
    if not latest_date or not last_seen:
        return "不明"

    latest = datetime.strptime(latest_date, "%Y-%m-%d")
    last = datetime.strptime(last_seen, "%Y-%m-%d")
    days_absent = (latest - last).days

    if first_seen:
        first = datetime.strptime(first_seen, "%Y-%m-%d")
        if (latest - first).days <= 30:
            return "新規"

    if days_absent >= 60:
        return "休眠候補"

    if recent_stream_count > 0 and previous_stream_count == 0:
        return "復帰"

    if recent_stream_count > 0:
        return "継続中"

    return "低頻度"

def get_summary() -> dict[str, Any]:
    owner_ph = placeholders(OWNER_CHANNEL_IDS)
    owner_params = tuple(OWNER_CHANNEL_IDS)

    with connect() as conn:
        total_streams = conn.execute("SELECT COUNT(*) AS c FROM streams").fetchone()["c"]
        total_listeners = conn.execute(
            f"SELECT COUNT(*) AS c FROM listeners WHERE channel_id NOT IN ({owner_ph})",
            owner_params,
        ).fetchone()["c"]
        total_comments = conn.execute(
            f"SELECT COUNT(*) AS c FROM messages WHERE channel_id NOT IN ({owner_ph})",
            owner_params,
        ).fetchone()["c"]
        latest_date = latest_stream_date(conn)

        inactive = 0
        if latest_date:
            inactive = conn.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM listeners
                WHERE channel_id NOT IN ({owner_ph})
                  AND last_seen_date IS NOT NULL
                  AND CAST(julianday(?) - julianday(last_seen_date) AS INTEGER) >= 30
                """,
                (latest_date, *owner_params),
            ).fetchone()["c"]

        top = conn.execute(
            f"""
            SELECT
                l.latest_display_name AS display_name,
                l.channel_id,
                COUNT(DISTINCT m.video_id) AS stream_count,
                COUNT(m.message_id) AS comment_count,
                ROUND(
                    CAST(COUNT(m.message_id) AS REAL) /
                    NULLIF(COUNT(DISTINCT m.video_id), 0), 1
                ) AS avg_comments,
                l.first_seen_date,
                l.last_seen_date
            FROM listeners l
            JOIN messages m ON m.channel_id = l.channel_id
            WHERE l.channel_id NOT IN ({owner_ph})
            GROUP BY l.channel_id
            ORDER BY stream_count DESC, comment_count DESC
            LIMIT 10
            """,
            owner_params,
        ).fetchall()

    # Listener lifecycle counts for the dashboard.
    status_counts = {
        "super_regular": 0,
        "regular": 0,
        "new": 0,
        "returning": 0,
        "dormant": 0,
    }

    with connect() as conn:
        listeners = conn.execute(
            f"""
            SELECT
                l.channel_id,
                l.first_seen_date,
                l.last_seen_date,
                COUNT(DISTINCT m.video_id) AS stream_count
            FROM listeners l
            JOIN messages m ON m.channel_id = l.channel_id
            WHERE l.channel_id NOT IN ({owner_ph})
            GROUP BY l.channel_id
            """,
            owner_params,
        ).fetchall()

        for row in listeners:
            participation_rate = row["stream_count"] / total_streams if total_streams else 0
            if participation_rate >= 0.5:
                status_counts["super_regular"] += 1
            elif participation_rate >= 0.25:
                status_counts["regular"] += 1

            if latest_date and row["first_seen_date"]:
                first_days = (
                    datetime.strptime(latest_date, "%Y-%m-%d")
                    - datetime.strptime(row["first_seen_date"], "%Y-%m-%d")
                ).days
                if first_days <= 30:
                    status_counts["new"] += 1

            if latest_date and row["last_seen_date"]:
                absent_days = (
                    datetime.strptime(latest_date, "%Y-%m-%d")
                    - datetime.strptime(row["last_seen_date"], "%Y-%m-%d")
                ).days
                if absent_days >= 60:
                    status_counts["dormant"] += 1

                recent_count = conn.execute(
                    """
                    SELECT COUNT(DISTINCT m.video_id) AS c
                    FROM messages m
                    JOIN streams s ON s.video_id = m.video_id
                    WHERE m.channel_id = ?
                      AND s.stream_date >= date(?, '-30 day')
                    """,
                    (row["channel_id"], latest_date),
                ).fetchone()["c"]

                previous_count = conn.execute(
                    """
                    SELECT COUNT(DISTINCT m.video_id) AS c
                    FROM messages m
                    JOIN streams s ON s.video_id = m.video_id
                    WHERE m.channel_id = ?
                      AND s.stream_date >= date(?, '-60 day')
                      AND s.stream_date < date(?, '-30 day')
                    """,
                    (row["channel_id"], latest_date, latest_date),
                ).fetchone()["c"]

                if recent_count > 0 and previous_count == 0 and first_days > 30:
                    status_counts["returning"] += 1

    return {
        "total_streams": total_streams,
        "total_listeners": total_listeners,
        "total_comments": total_comments,
        "latest_date": latest_date,
        "inactive_30": inactive,
        "top_listeners": [dict(r) for r in top],
        "status_counts": status_counts,
    }

def get_categories() -> list[dict[str, Any]]:
    owner_ph = placeholders(OWNER_CHANNEL_IDS)
    owner_params = tuple(OWNER_CHANNEL_IDS)

    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                CASE WHEN s.category IS NULL OR s.category = '' OR s.category = 'default_category' THEN 'その他' ELSE s.category END AS category,
                COUNT(DISTINCT s.video_id) AS stream_count,
                ROUND(AVG(x.participants), 1) AS avg_participants,
                ROUND(AVG(x.comments), 1) AS avg_comments,
                MAX(x.participants) AS max_participants,
                MAX(x.comments) AS max_comments
            FROM streams s
            LEFT JOIN (
                SELECT
                    video_id,
                    COUNT(DISTINCT CASE
                        WHEN channel_id NOT IN ({owner_ph}) THEN channel_id
                    END) AS participants,
                    COUNT(CASE
                        WHEN channel_id NOT IN ({owner_ph}) THEN message_id
                    END) AS comments
                FROM messages
                GROUP BY video_id
            ) x ON x.video_id = s.video_id
            GROUP BY CASE WHEN s.category IS NULL OR s.category = '' OR s.category = 'default_category' THEN 'その他' ELSE s.category END
            ORDER BY avg_participants DESC, avg_comments DESC
            """,
            owner_params * 2,
        ).fetchall()

    return [dict(r) for r in rows]

def get_weekdays() -> list[dict[str, Any]]:
    owner_ph = placeholders(OWNER_CHANNEL_IDS)
    owner_params = tuple(OWNER_CHANNEL_IDS)
    order = {"月": 1, "火": 2, "水": 3, "木": 4, "金": 5, "土": 6, "日": 7}

    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                COALESCE(s.weekday, '') AS weekday,
                COUNT(DISTINCT s.video_id) AS stream_count,
                ROUND(AVG(x.participants), 1) AS avg_participants,
                ROUND(AVG(x.comments), 1) AS avg_comments
            FROM streams s
            LEFT JOIN (
                SELECT
                    video_id,
                    COUNT(DISTINCT CASE
                        WHEN channel_id NOT IN ({owner_ph}) THEN channel_id
                    END) AS participants,
                    COUNT(CASE
                        WHEN channel_id NOT IN ({owner_ph}) THEN message_id
                    END) AS comments
                FROM messages
                GROUP BY video_id
            ) x ON x.video_id = s.video_id
            GROUP BY s.weekday
            """,
            owner_params * 2,
        ).fetchall()

    data = [dict(r) for r in rows if r["weekday"]]
    data.sort(key=lambda r: order.get(r["weekday"], 99))
    return data

def get_recent_streams(limit: int = 30) -> list[dict[str, Any]]:
    owner_ph = placeholders(OWNER_CHANNEL_IDS)
    owner_params = tuple(OWNER_CHANNEL_IDS)

    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                s.stream_date,
                s.video_id,
                s.title,
                CASE WHEN s.category IS NULL OR s.category = '' OR s.category = 'default_category' THEN 'その他' ELSE s.category END AS category,
                COALESCE(s.weekday, '') AS weekday,
                COUNT(DISTINCT CASE
                    WHEN m.channel_id NOT IN ({owner_ph}) THEN m.channel_id
                END) AS participants,
                COUNT(CASE
                    WHEN m.channel_id NOT IN ({owner_ph}) THEN m.message_id
                END) AS comments
            FROM streams s
            LEFT JOIN messages m ON m.video_id = s.video_id
            GROUP BY s.video_id
            ORDER BY s.stream_date DESC, s.imported_at DESC
            LIMIT ?
            """,
            (*owner_params, *owner_params, limit),
        ).fetchall()

    return [dict(r) for r in rows]


def grade_stream(
    participants: int,
    comments: int,
    participant_avg: float,
    comment_avg: float,
) -> str:
    participant_ratio = participants / participant_avg if participant_avg else 1
    comment_ratio = comments / comment_avg if comment_avg else 1
    score = participant_ratio * 0.55 + comment_ratio * 0.45

    if score >= 1.35:
        return "S"
    if score >= 1.15:
        return "A"
    if score >= 0.95:
        return "B"
    if score >= 0.75:
        return "C"
    return "D"


def build_stream_notes(
    participants: int,
    comments: int,
    new_count: int,
    returning_count: int,
    previous_participants: int | None,
    previous_comments: int | None,
    category_avg_participants: float,
    category_avg_comments: float,
) -> dict[str, list[str]]:
    positives: list[str] = []
    cautions: list[str] = []
    actions: list[str] = []

    if previous_participants is not None:
        diff = participants - previous_participants
        if diff > 0:
            positives.append(f"コメント参加人数が前回より{diff}人増えています。")
        elif diff < 0:
            cautions.append(f"コメント参加人数が前回より{abs(diff)}人少なめです。")

    if previous_comments is not None:
        diff = comments - previous_comments
        if diff > 0:
            positives.append(f"コメント数が前回より{diff}件増えています。")
        elif diff < 0:
            cautions.append(f"コメント数が前回より{abs(diff)}件少なめです。")

    if new_count > 0:
        positives.append(f"新規コメント参加者が{new_count}人いました。")
        actions.append("新規参加者が次回も入りやすいよう、次回予告や案内導線を分かりやすくすると良さそうです。")

    if returning_count > 0:
        positives.append(f"30日以上ぶりに戻ったリスナーが{returning_count}人いました。")

    if category_avg_participants:
        ratio = participants / category_avg_participants
        if ratio >= 1.15:
            positives.append("同じ企画の平均よりコメント参加人数が好調です。")
        elif ratio <= 0.85:
            cautions.append("同じ企画の平均よりコメント参加人数が少なめです。")
            actions.append("タイトル、告知時刻、開始時刻など、企画以外の条件も見直す候補です。")

    if category_avg_comments:
        ratio = comments / category_avg_comments
        if ratio >= 1.15:
            positives.append("同じ企画の平均よりコメントが活発です。")
        elif ratio <= 0.85:
            cautions.append("同じ企画の平均よりコメント数が少なめです。")

    if not positives:
        positives.append("大きな数値変動はありませんでした。")
    if not cautions:
        cautions.append("大きな注意点は確認されませんでした。")
    if not actions:
        actions.append("今回の条件を記録し、同じ企画の次回配信と比較してください。")

    return {
        "positives": positives,
        "cautions": cautions,
        "actions": actions,
    }


def get_stream(video_id: str) -> dict[str, Any] | None:
    owner_ph = placeholders(OWNER_CHANNEL_IDS)
    owner_params = tuple(OWNER_CHANNEL_IDS)

    with connect() as conn:
        stream = conn.execute(
            """
            SELECT
                video_id,
                stream_date,
                title,
                COALESCE(category, 'その他') AS category,
                COALESCE(weekday, '') AS weekday
            FROM streams
            WHERE video_id = ?
            """,
            (video_id,),
        ).fetchone()

        if not stream:
            return None

        stats = conn.execute(
            f"""
            SELECT
                COUNT(DISTINCT CASE
                    WHEN channel_id NOT IN ({owner_ph}) THEN channel_id
                END) AS participants,
                COUNT(CASE
                    WHEN channel_id NOT IN ({owner_ph}) THEN message_id
                END) AS comments
            FROM messages
            WHERE video_id = ?
            """,
            (*owner_params, *owner_params, video_id),
        ).fetchone()

        top = conn.execute(
            f"""
            SELECT
                l.latest_display_name AS display_name,
                m.channel_id,
                COUNT(*) AS comment_count
            FROM messages m
            JOIN listeners l ON l.channel_id = m.channel_id
            WHERE m.video_id = ?
              AND m.channel_id NOT IN ({owner_ph})
            GROUP BY m.channel_id
            ORDER BY comment_count DESC
            LIMIT 10
            """,
            (video_id, *owner_params),
        ).fetchall()

        new_people = conn.execute(
            f"""
            SELECT
                l.latest_display_name AS display_name,
                l.channel_id
            FROM listeners l
            WHERE l.first_seen_date = ?
              AND l.channel_id NOT IN ({owner_ph})
            ORDER BY l.latest_display_name
            """,
            (stream["stream_date"], *owner_params),
        ).fetchall()

        returning_people = conn.execute(
            f"""
            SELECT DISTINCT
                l.latest_display_name AS display_name,
                l.channel_id
            FROM messages current_m
            JOIN listeners l ON l.channel_id = current_m.channel_id
            WHERE current_m.video_id = ?
              AND current_m.channel_id NOT IN ({owner_ph})
              AND EXISTS (
                  SELECT 1
                  FROM messages old_m
                  JOIN streams old_s ON old_s.video_id = old_m.video_id
                  WHERE old_m.channel_id = current_m.channel_id
                    AND old_s.stream_date < date(?, '-30 day')
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM messages recent_m
                  JOIN streams recent_s ON recent_s.video_id = recent_m.video_id
                  WHERE recent_m.channel_id = current_m.channel_id
                    AND recent_s.stream_date >= date(?, '-30 day')
                    AND recent_s.stream_date < ?
              )
            ORDER BY l.latest_display_name
            """,
            (
                video_id,
                *owner_params,
                stream["stream_date"],
                stream["stream_date"],
                stream["stream_date"],
            ),
        ).fetchall()

        previous = conn.execute(
            f"""
            SELECT
                s.video_id,
                s.stream_date,
                s.title,
                COUNT(DISTINCT CASE
                    WHEN m.channel_id NOT IN ({owner_ph}) THEN m.channel_id
                END) AS participants,
                COUNT(CASE
                    WHEN m.channel_id NOT IN ({owner_ph}) THEN m.message_id
                END) AS comments
            FROM streams s
            LEFT JOIN messages m ON m.video_id = s.video_id
            WHERE s.stream_date < ?
            GROUP BY s.video_id
            ORDER BY s.stream_date DESC, s.imported_at DESC
            LIMIT 1
            """,
            (*owner_params, *owner_params, stream["stream_date"]),
        ).fetchone()

        category_avg = conn.execute(
            f"""
            SELECT
                ROUND(AVG(x.participants), 1) AS avg_participants,
                ROUND(AVG(x.comments), 1) AS avg_comments
            FROM streams s
            LEFT JOIN (
                SELECT
                    video_id,
                    COUNT(DISTINCT CASE
                        WHEN channel_id NOT IN ({owner_ph}) THEN channel_id
                    END) AS participants,
                    COUNT(CASE
                        WHEN channel_id NOT IN ({owner_ph}) THEN message_id
                    END) AS comments
                FROM messages
                GROUP BY video_id
            ) x ON x.video_id = s.video_id
            WHERE CASE WHEN s.category IS NULL OR s.category = '' OR s.category = 'default_category' THEN 'その他' ELSE s.category END = ?
            """,
            (*owner_params, *owner_params, stream["category"]),
        ).fetchone()

        overall_avg = conn.execute(
            f"""
            SELECT
                ROUND(AVG(x.participants), 1) AS avg_participants,
                ROUND(AVG(x.comments), 1) AS avg_comments
            FROM streams s
            LEFT JOIN (
                SELECT
                    video_id,
                    COUNT(DISTINCT CASE
                        WHEN channel_id NOT IN ({owner_ph}) THEN channel_id
                    END) AS participants,
                    COUNT(CASE
                        WHEN channel_id NOT IN ({owner_ph}) THEN message_id
                    END) AS comments
                FROM messages
                GROUP BY video_id
            ) x ON x.video_id = s.video_id
            """,
            owner_params * 2,
        ).fetchone()

    participants = int(stats["participants"] or 0)
    comments = int(stats["comments"] or 0)
    category_avg_participants = float(category_avg["avg_participants"] or 0)
    category_avg_comments = float(category_avg["avg_comments"] or 0)

    grade = grade_stream(
        participants,
        comments,
        float(overall_avg["avg_participants"] or 0),
        float(overall_avg["avg_comments"] or 0),
    )

    notes = build_stream_notes(
        participants=participants,
        comments=comments,
        new_count=len(new_people),
        returning_count=len(returning_people),
        previous_participants=int(previous["participants"]) if previous else None,
        previous_comments=int(previous["comments"]) if previous else None,
        category_avg_participants=category_avg_participants,
        category_avg_comments=category_avg_comments,
    )

    return {
        "stream": dict(stream),
        "stats": dict(stats),
        "top_commenters": [dict(r) for r in top],
        "new_listeners": [dict(r) for r in new_people],
        "returning_listeners": [dict(r) for r in returning_people],
        "previous_stream": dict(previous) if previous else None,
        "category_average": dict(category_avg),
        "overall_average": dict(overall_avg),
        "grade": grade,
        "notes": notes,
    }


def search_listeners(query: str, limit: int = 100) -> list[dict[str, Any]]:
    owner_ph = placeholders(OWNER_CHANNEL_IDS)
    owner_params = tuple(OWNER_CHANNEL_IDS)
    q = f"%{query}%"

    with connect() as conn:
        total_streams = conn.execute("SELECT COUNT(*) AS c FROM streams").fetchone()["c"]
        latest_date = latest_stream_date(conn)

        rows = conn.execute(
            f"""
            SELECT
                l.latest_display_name AS display_name,
                l.channel_id,
                COUNT(DISTINCT m.video_id) AS stream_count,
                COUNT(m.message_id) AS comment_count,
                ROUND(
                    CAST(COUNT(m.message_id) AS REAL) /
                    NULLIF(COUNT(DISTINCT m.video_id), 0), 1
                ) AS avg_comments,
                l.first_seen_date,
                l.last_seen_date,
                COUNT(DISTINCT substr(s.stream_date, 1, 7)) AS active_months
            FROM listeners l
            JOIN messages m ON m.channel_id = l.channel_id
            JOIN streams s ON s.video_id = m.video_id
            WHERE l.channel_id NOT IN ({owner_ph})
              AND (
                  l.latest_display_name LIKE ?
                  OR l.channel_id LIKE ?
                  OR EXISTS (
                      SELECT 1
                      FROM listener_names n
                      WHERE n.channel_id = l.channel_id
                        AND n.display_name LIKE ?
                  )
              )
            GROUP BY l.channel_id
            ORDER BY stream_count DESC, comment_count DESC
            LIMIT ?
            """,
            (*owner_params, q, q, q, limit),
        ).fetchall()

        result = []
        for row in rows:
            item = dict(row)
            participation_rate = item["stream_count"] / total_streams if total_streams else 0
            days_absent = None
            if latest_date and item["last_seen_date"]:
                days_absent = (
                    datetime.strptime(latest_date, "%Y-%m-%d")
                    - datetime.strptime(item["last_seen_date"], "%Y-%m-%d")
                ).days

            item["participation_rate"] = participation_rate
            item["fan_score"] = calculate_fan_score(
                participation_rate,
                item["avg_comments"] or 0,
                days_absent,
                item["active_months"] or 0,
            )

            if participation_rate >= 0.5:
                item["listener_type"] = "超常連"
            elif participation_rate >= 0.25:
                item["listener_type"] = "常連"
            elif participation_rate >= 0.1:
                item["listener_type"] = "準常連"
            else:
                item["listener_type"] = "スポット参加"

            if item["first_seen_date"] and latest_date:
                first_days = (
                    datetime.strptime(latest_date, "%Y-%m-%d")
                    - datetime.strptime(item["first_seen_date"], "%Y-%m-%d")
                ).days
            else:
                first_days = None

            if first_days is not None and first_days <= 30:
                item["status"] = "新規"
            elif days_absent is not None and days_absent >= 60:
                item["status"] = "休眠候補"
            elif days_absent is not None and days_absent <= 14:
                item["status"] = "継続中"
            else:
                item["status"] = "低頻度"

            item["days_absent"] = days_absent
            result.append(item)

    return result

def get_listener(channel_id: str) -> dict[str, Any] | None:
    if channel_id in OWNER_CHANNEL_IDS:
        return None

    with connect() as conn:
        total_streams = conn.execute("SELECT COUNT(*) AS c FROM streams").fetchone()["c"]
        latest_date = latest_stream_date(conn)

        profile = conn.execute(
            """
            SELECT
                l.latest_display_name AS display_name,
                l.channel_id,
                l.first_seen_date,
                l.last_seen_date,
                COUNT(DISTINCT m.video_id) AS stream_count,
                COUNT(m.message_id) AS comment_count,
                ROUND(
                    CAST(COUNT(m.message_id) AS REAL) /
                    NULLIF(COUNT(DISTINCT m.video_id), 0), 1
                ) AS avg_comments,
                COUNT(DISTINCT substr(s.stream_date, 1, 7)) AS active_months
            FROM listeners l
            JOIN messages m ON m.channel_id = l.channel_id
            JOIN streams s ON s.video_id = m.video_id
            WHERE l.channel_id = ?
            GROUP BY l.channel_id
            """,
            (channel_id,),
        ).fetchone()

        if not profile:
            return None

        names = conn.execute(
            """
            SELECT display_name, first_seen_date, last_seen_date
            FROM listener_names
            WHERE channel_id = ?
            ORDER BY last_seen_date DESC
            """,
            (channel_id,),
        ).fetchall()

        recent = conn.execute(
            """
            SELECT
                s.stream_date,
                s.video_id,
                s.title,
                CASE WHEN s.category IS NULL OR s.category = '' OR s.category = 'default_category' THEN 'その他' ELSE s.category END AS category,
                COUNT(m.message_id) AS comment_count
            FROM streams s
            JOIN messages m ON m.video_id = s.video_id
            WHERE m.channel_id = ?
            GROUP BY s.video_id
            ORDER BY s.stream_date DESC, s.imported_at DESC
            LIMIT 20
            """,
            (channel_id,),
        ).fetchall()

        monthly = conn.execute(
            """
            SELECT
                substr(s.stream_date, 1, 7) AS month,
                COUNT(DISTINCT s.video_id) AS stream_count,
                COUNT(m.message_id) AS comment_count
            FROM streams s
            JOIN messages m ON m.video_id = s.video_id
            WHERE m.channel_id = ?
              AND s.stream_date IS NOT NULL
            GROUP BY substr(s.stream_date, 1, 7)
            ORDER BY month
            """,
            (channel_id,),
        ).fetchall()

        recent_window = conn.execute(
            """
            SELECT COUNT(DISTINCT m.video_id) AS c
            FROM messages m
            JOIN streams s ON s.video_id = m.video_id
            WHERE m.channel_id = ?
              AND s.stream_date >= date(?, '-30 day')
            """,
            (channel_id, latest_date),
        ).fetchone()["c"] if latest_date else 0

        previous_window = conn.execute(
            """
            SELECT COUNT(DISTINCT m.video_id) AS c
            FROM messages m
            JOIN streams s ON s.video_id = m.video_id
            WHERE m.channel_id = ?
              AND s.stream_date >= date(?, '-60 day')
              AND s.stream_date < date(?, '-30 day')
            """,
            (channel_id, latest_date, latest_date),
        ).fetchone()["c"] if latest_date else 0

    data = dict(profile)
    participation_rate = data["stream_count"] / total_streams if total_streams else 0
    days_absent = None
    if latest_date and data["last_seen_date"]:
        days_absent = (
            datetime.strptime(latest_date, "%Y-%m-%d")
            - datetime.strptime(data["last_seen_date"], "%Y-%m-%d")
        ).days

    if participation_rate >= 0.5:
        listener_type = "超常連"
    elif participation_rate >= 0.25:
        listener_type = "常連"
    elif participation_rate >= 0.1:
        listener_type = "準常連"
    else:
        listener_type = "スポット参加"

    data["participation_rate"] = participation_rate
    data["listener_type"] = listener_type
    data["days_absent"] = days_absent
    data["fan_score"] = calculate_fan_score(
        participation_rate,
        data["avg_comments"] or 0,
        days_absent,
        data["active_months"] or 0,
    )
    data["status"] = classify_status(
        data["first_seen_date"],
        data["last_seen_date"],
        latest_date,
        recent_window,
        previous_window,
    )

    return {
        "profile": data,
        "names": [dict(r) for r in names],
        "recent_streams": [dict(r) for r in recent],
        "monthly": [dict(r) for r in monthly],
    }


def get_community_analysis() -> dict[str, Any]:
    owner_ph = placeholders(OWNER_CHANNEL_IDS)
    owner_params = tuple(OWNER_CHANNEL_IDS)

    with connect() as conn:
        latest_date = latest_stream_date(conn)
        total_streams = conn.execute(
            "SELECT COUNT(*) AS c FROM streams"
        ).fetchone()["c"]

        listeners = conn.execute(
            f"""
            SELECT
                l.channel_id,
                l.latest_display_name AS display_name,
                l.first_seen_date,
                l.last_seen_date,
                COUNT(DISTINCT m.video_id) AS stream_count,
                COUNT(m.message_id) AS comment_count,
                COUNT(DISTINCT substr(s.stream_date, 1, 7)) AS active_months
            FROM listeners l
            JOIN messages m ON m.channel_id = l.channel_id
            JOIN streams s ON s.video_id = m.video_id
            WHERE l.channel_id NOT IN ({owner_ph})
            GROUP BY l.channel_id
            """,
            owner_params,
        ).fetchall()

        total_listeners = len(listeners)
        active_30 = 0
        dormant_60 = 0
        core_count = 0
        new_30 = 0

        for row in listeners:
            participation_rate = row["stream_count"] / total_streams if total_streams else 0
            if participation_rate >= 0.25:
                core_count += 1

            if latest_date and row["last_seen_date"]:
                days_absent = (
                    datetime.strptime(latest_date, "%Y-%m-%d")
                    - datetime.strptime(row["last_seen_date"], "%Y-%m-%d")
                ).days
                if days_absent <= 30:
                    active_30 += 1
                if days_absent >= 60:
                    dormant_60 += 1

            if latest_date and row["first_seen_date"]:
                first_days = (
                    datetime.strptime(latest_date, "%Y-%m-%d")
                    - datetime.strptime(row["first_seen_date"], "%Y-%m-%d")
                ).days
                if first_days <= 30:
                    new_30 += 1

        active_rate = active_30 / total_listeners if total_listeners else 0
        core_rate = core_count / total_listeners if total_listeners else 0
        dormant_rate = dormant_60 / total_listeners if total_listeners else 0

        # Health score: transparent rule-based score, not a medical or objective metric.
        health_score = round(
            min(
                100,
                active_rate * 45
                + min(core_rate / 0.30, 1) * 30
                + max(0, 1 - dormant_rate) * 25
            )
        )

        monthly_rows = conn.execute(
            f"""
            SELECT
                substr(s.stream_date, 1, 7) AS month,
                COUNT(DISTINCT s.video_id) AS stream_count,
                COUNT(DISTINCT CASE
                    WHEN m.channel_id NOT IN ({owner_ph}) THEN m.channel_id
                END) AS active_listeners,
                COUNT(CASE
                    WHEN m.channel_id NOT IN ({owner_ph}) THEN m.message_id
                END) AS comments
            FROM streams s
            LEFT JOIN messages m ON m.video_id = s.video_id
            WHERE s.stream_date IS NOT NULL
            GROUP BY substr(s.stream_date, 1, 7)
            ORDER BY month
            """,
            owner_params * 2,
        ).fetchall()

        new_by_month = conn.execute(
            f"""
            SELECT
                substr(first_seen_date, 1, 7) AS month,
                COUNT(*) AS new_listeners
            FROM listeners
            WHERE channel_id NOT IN ({owner_ph})
              AND first_seen_date IS NOT NULL
            GROUP BY substr(first_seen_date, 1, 7)
            ORDER BY month
            """,
            owner_params,
        ).fetchall()

        new_map = {r["month"]: r["new_listeners"] for r in new_by_month}
        monthly = []
        for row in monthly_rows:
            item = dict(row)
            item["new_listeners"] = new_map.get(row["month"], 0)
            monthly.append(item)

        # Retention: listeners whose first month is known, and whether they returned in a later month.
        retention_rows = conn.execute(
            f"""
            SELECT
                l.channel_id,
                substr(l.first_seen_date, 1, 7) AS first_month,
                COUNT(DISTINCT CASE
                    WHEN substr(s.stream_date, 1, 7) > substr(l.first_seen_date, 1, 7)
                    THEN substr(s.stream_date, 1, 7)
                END) AS later_months
            FROM listeners l
            JOIN messages m ON m.channel_id = l.channel_id
            JOIN streams s ON s.video_id = m.video_id
            WHERE l.channel_id NOT IN ({owner_ph})
              AND l.first_seen_date IS NOT NULL
            GROUP BY l.channel_id
            """,
            owner_params,
        ).fetchall()

        cohort_total = len(retention_rows)
        retained = sum(1 for r in retention_rows if r["later_months"] > 0)
        retention_rate = retained / cohort_total if cohort_total else 0

        # Co-attendance pairs: top listeners only to keep query/results practical.
        top_ids = [
            r["channel_id"]
            for r in conn.execute(
                f"""
                SELECT m.channel_id, COUNT(DISTINCT m.video_id) AS stream_count
                FROM messages m
                WHERE m.channel_id NOT IN ({owner_ph})
                GROUP BY m.channel_id
                ORDER BY stream_count DESC
                LIMIT 30
                """,
                owner_params,
            ).fetchall()
        ]

        pair_rows = []
        if top_ids:
            top_ph = ",".join("?" for _ in top_ids)
            raw_pairs = conn.execute(
                f"""
                SELECT
                    a.channel_id AS channel_a,
                    b.channel_id AS channel_b,
                    COUNT(DISTINCT a.video_id) AS shared_streams
                FROM messages a
                JOIN messages b
                  ON a.video_id = b.video_id
                 AND a.channel_id < b.channel_id
                WHERE a.channel_id IN ({top_ph})
                  AND b.channel_id IN ({top_ph})
                GROUP BY a.channel_id, b.channel_id
                HAVING shared_streams >= 3
                ORDER BY shared_streams DESC
                LIMIT 20
                """,
                (*top_ids, *top_ids),
            ).fetchall()

            names = {
                r["channel_id"]: r["latest_display_name"]
                for r in conn.execute(
                    f"""
                    SELECT channel_id, latest_display_name
                    FROM listeners
                    WHERE channel_id IN ({top_ph})
                    """,
                    top_ids,
                ).fetchall()
            }

            counts = {
                r["channel_id"]: r["stream_count"]
                for r in conn.execute(
                    f"""
                    SELECT channel_id, COUNT(DISTINCT video_id) AS stream_count
                    FROM messages
                    WHERE channel_id IN ({top_ph})
                    GROUP BY channel_id
                    """,
                    top_ids,
                ).fetchall()
            }

            for row in raw_pairs:
                a = row["channel_a"]
                b = row["channel_b"]
                shared = row["shared_streams"]
                denominator = min(counts.get(a, 1), counts.get(b, 1))
                affinity = shared / denominator if denominator else 0
                pair_rows.append({
                    "name_a": names.get(a, a),
                    "channel_a": a,
                    "name_b": names.get(b, b),
                    "channel_b": b,
                    "shared_streams": shared,
                    "affinity": affinity,
                })

        notes: list[str] = []
        if active_rate >= 0.6:
            notes.append(
                "直近30日の活動率はかなり良好です。今の運用は機能しています。"
                "ただし、常連だけで回っていないか、新規の定着も合わせて確認してください。"
            )
        elif active_rate >= 0.35:
            notes.append(
                "活動率は悪くありませんが、『順調』と言い切るには弱い数字です。"
                "配信頻度、告知時刻、定番企画の再現性を見直す余地があります。"
            )
        else:
            notes.append(
                "活動率は正直かなり低めです。配信を続けるだけでは戻りません。"
                "初見が入りやすい説明、次回予告、定期企画の固定化から立て直してください。"
            )

        if retention_rate >= 0.6:
            notes.append(
                "初参加者の定着は強いです。初見を置いていかない空気が作れています。"
                "この良さを崩さず、常連同士だけの内輪化には注意してください。"
            )
        elif retention_rate >= 0.35:
            notes.append(
                "定着率は中間です。来てくれた人を逃してはいませんが、"
                "次も来る理由を十分に作れているとも言えません。次回予定を明確にしましょう。"
            )
        else:
            notes.append(
                "初見が一度きりで終わる割合が高めです。"
                "配信内容よりも、初見への案内・参加方法・次回導線に穴がある可能性があります。"
            )

        if dormant_rate >= 0.4:
            notes.append(
                "休眠率は高めです。『自然に戻ってくるだろう』は期待しない方がいいです。"
                "久しぶりでも入りやすい企画や、過去参加者にも届く告知を検討してください。"
            )
        elif dormant_rate <= 0.2:
            notes.append(
                "長期休眠は比較的少なく、コミュニティの維持力があります。"
                "今の距離感を守りつつ、固定メンバー依存にならないよう新規導線も残してください。"
            )

    return {
        "health_score": health_score,
        "total_listeners": total_listeners,
        "active_30": active_30,
        "active_rate": active_rate,
        "core_count": core_count,
        "core_rate": core_rate,
        "new_30": new_30,
        "dormant_60": dormant_60,
        "dormant_rate": dormant_rate,
        "retained": retained,
        "cohort_total": cohort_total,
        "retention_rate": retention_rate,
        "monthly": monthly,
        "top_pairs": pair_rows,
        "notes": notes,
        "latest_date": latest_date,
    }



def get_moderation_candidates(
    query: str = "",
    status_filter: str = "",
    category_filter: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    rules = load_moderation_rules()
    owner_ph = placeholders(OWNER_CHANNEL_IDS)
    owner_params = tuple(OWNER_CHANNEL_IDS)

    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                m.message_id,
                m.video_id,
                m.channel_id,
                l.latest_display_name AS display_name,
                m.message_text,
                m.timestamp_text,
                s.stream_date,
                s.title,
                COALESCE(r.status, '未確認') AS review_status,
                COALESCE(r.category, '') AS review_category,
                COALESCE(r.memo, '') AS memo,
                COALESCE(r.reviewed_at, '') AS reviewed_at
            FROM messages m
            JOIN listeners l ON l.channel_id = m.channel_id
            JOIN streams s ON s.video_id = m.video_id
            LEFT JOIN moderation_reviews r ON r.message_id = m.message_id
            WHERE m.channel_id NOT IN ({owner_ph})
              AND m.message_text IS NOT NULL
              AND m.message_text <> ''
            ORDER BY s.stream_date DESC, m.timestamp_usec DESC
            LIMIT 5000
            """,
            owner_params,
        ).fetchall()

    result: list[dict[str, Any]] = []
    q = query.casefold().strip()

    for row in rows:
        item = dict(row)
        matches = detect_rule_matches(item["message_text"], rules)
        if not matches and item["review_status"] == "未確認":
            continue

        item["matches"] = matches
        item["detected_category"] = matches[0]["category"] if matches else ""
        item["detected_label"] = matches[0]["label"] if matches else ""
        item["detected_rule"] = matches[0]["rule"] if matches else ""

        if q and q not in item["display_name"].casefold() and q not in item["message_text"].casefold():
            continue
        if status_filter and item["review_status"] != status_filter:
            continue
        if not status_filter and item["review_status"] == "問題なし":
            continue
        effective_category = item["review_category"] or item["detected_category"]
        if category_filter and effective_category != category_filter:
            continue

        result.append(item)
        if len(result) >= limit:
            break

    return result

def get_message_context(message_id: str, radius: int = 3) -> dict[str, Any] | None:
    ensure_moderation_storage()
    with connect() as conn:
        target = conn.execute(
            """
            SELECT
                m.message_id,
                m.video_id,
                m.channel_id,
                l.latest_display_name AS display_name,
                m.message_text,
                m.timestamp_text,
                m.timestamp_usec,
                s.stream_date,
                s.title,
                COALESCE(r.status, '未確認') AS review_status,
                COALESCE(r.category, '') AS review_category,
                COALESCE(r.memo, '') AS memo,
                COALESCE(r.reviewed_at, '') AS reviewed_at
            FROM messages m
            JOIN listeners l ON l.channel_id = m.channel_id
            JOIN streams s ON s.video_id = m.video_id
            LEFT JOIN moderation_reviews r ON r.message_id = m.message_id
            WHERE m.message_id = ?
            """,
            (message_id,),
        ).fetchone()
        if not target:
            return None

        all_rows = conn.execute(
            """
            SELECT
                m.message_id,
                m.channel_id,
                l.latest_display_name AS display_name,
                m.message_text,
                m.timestamp_text,
                m.timestamp_usec
            FROM messages m
            JOIN listeners l ON l.channel_id = m.channel_id
            WHERE m.video_id = ?
            ORDER BY
                CASE
                    WHEN m.timestamp_usec GLOB '[0-9]*' THEN CAST(m.timestamp_usec AS INTEGER)
                    ELSE 0
                END,
                m.message_id
            """,
            (target["video_id"],),
        ).fetchall()

        ids = [r["message_id"] for r in all_rows]
        try:
            index = ids.index(message_id)
        except ValueError:
            index = 0
        start = max(0, index - radius)
        end = min(len(all_rows), index + radius + 1)
        context = [dict(r) for r in all_rows[start:end]]

        history = conn.execute(
            """
            SELECT
                m.message_id,
                s.stream_date,
                s.title,
                m.message_text,
                COALESCE(r.status, '未確認') AS review_status,
                COALESCE(r.category, '') AS review_category,
                COALESCE(r.memo, '') AS memo
            FROM moderation_reviews r
            JOIN messages m ON m.message_id = r.message_id
            JOIN streams s ON s.video_id = m.video_id
            WHERE m.channel_id = ?
            ORDER BY s.stream_date DESC
            LIMIT 50
            """,
            (target["channel_id"],),
        ).fetchall()

    rules = load_moderation_rules()
    result = dict(target)
    result["matches"] = detect_rule_matches(result["message_text"], rules)
    return {
        "target": result,
        "context": context,
        "history": [dict(r) for r in history],
    }

def save_moderation_review(
    message_id: str,
    status: str,
    category: str,
    memo: str,
) -> dict[str, Any]:
    ensure_moderation_storage()
    allowed_statuses = {
        "未確認", "問題なし", "注意", "セクハラ",
        "暴言", "距離感", "荒らし", "対応済み"
    }
    if status not in allowed_statuses:
        raise ValueError("invalid moderation status")

    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if not exists:
            raise ValueError("message not found")

        conn.execute(
            """
            INSERT INTO moderation_reviews(
                message_id, status, category, memo, reviewed_at
            )
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(message_id) DO UPDATE SET
                status = excluded.status,
                category = excluded.category,
                memo = excluded.memo,
                reviewed_at = CURRENT_TIMESTAMP
            """,
            (message_id, status, category, memo),
        )
        conn.commit()

    return {"ok": True}

def export_moderation_csv() -> bytes:
    ensure_moderation_storage()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "配信日", "配信タイトル", "動画ID", "発言時刻",
        "表示名", "チャンネルID", "コメント", "判定",
        "カテゴリ", "対応メモ", "確認日時"
    ])

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                s.stream_date,
                s.title,
                m.video_id,
                m.timestamp_text,
                l.latest_display_name,
                m.channel_id,
                m.message_text,
                r.status,
                r.category,
                r.memo,
                r.reviewed_at
            FROM moderation_reviews r
            JOIN messages m ON m.message_id = r.message_id
            JOIN listeners l ON l.channel_id = m.channel_id
            JOIN streams s ON s.video_id = m.video_id
            WHERE r.status <> '未確認'
            ORDER BY s.stream_date DESC, r.reviewed_at DESC
            """
        ).fetchall()

    for row in rows:
        writer.writerow([
            row["stream_date"], row["title"], row["video_id"],
            row["timestamp_text"], row["latest_display_name"],
            row["channel_id"], row["message_text"], row["status"],
            row["category"], row["memo"], row["reviewed_at"],
        ])

    return output.getvalue().encode("utf-8-sig")


def get_app_info() -> dict[str, Any]:
    config = load_app_config()
    database_exists = DB_PATH.exists()
    stream_count = 0

    if database_exists:
        try:
            with connect() as conn:
                stream_count = conn.execute(
                    "SELECT COUNT(*) AS c FROM streams"
                ).fetchone()["c"]
        except Exception:
            database_exists = False

    owner_ids = [
        str(value).strip()
        for value in config.get("owner_channel_ids", [])
        if str(value).strip()
    ]
    config_ready = bool(
        config.get("channel_name")
        and owner_ids
        and owner_ids != ["YOUR_YOUTUBE_CHANNEL_ID"]
    )

    return {
       print("VTuber Analytics Web App v1.0.0")
        "app_name": config.get("app_name", "VTuber Analytics"),
        "powered_by": config.get("powered_by", "Aino Maria"),
        "channel_name": config.get("channel_name", "Aino Maria"),
        "theme_color": config.get("theme_color", "#24324A"),
        "database_exists": database_exists,
        "database_path": str(DB_PATH),
        "stream_count": stream_count,
        "config_ready": config_ready,
        "moderation_rules_exists": MODERATION_RULES_PATH.exists(),
    }

def public_app_config() -> dict[str, Any]:
    config = load_app_config()
    return {
        "app_name": "VTuber Analytics",
        "powered_by": "Aino Maria",
        "channel_name": str(config.get("channel_name", "Aino Maria")),
        "owner_channel_ids": list(config.get("owner_channel_ids", [])),
        "chat_data_dir": str(config.get(
            "chat_data_dir",
            PROJECT_DIR / "youtube_chat_data",
        )),
        "theme_color": str(config.get("theme_color", "#24324A")),
        "host": str(config.get("host", "127.0.0.1")),
        "port": int(config.get("port", 8765)),
    }

def save_app_config(payload: dict[str, Any]) -> dict[str, Any]:
    current = load_app_config()

    channel_name = str(
        payload.get("channel_name", current.get("channel_name", "Aino Maria"))
    ).strip()
    chat_data_dir = str(
        payload.get(
            "chat_data_dir",
            current.get("chat_data_dir", PROJECT_DIR / "youtube_chat_data"),
        )
    ).strip()
    theme_color = str(
        payload.get("theme_color", current.get("theme_color", "#24324A"))
    ).strip()

    owner_raw = payload.get(
        "owner_channel_ids",
        current.get("owner_channel_ids", []),
    )
    if isinstance(owner_raw, str):
        owner_ids = [v.strip() for v in owner_raw.split(",") if v.strip()]
    else:
        owner_ids = [str(v).strip() for v in owner_raw if str(v).strip()]

    if not channel_name:
        raise ValueError("チャンネル名を入力してください。")
    if not owner_ids:
        raise ValueError("本人のYouTubeチャンネルIDを入力してください。")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", theme_color):
        raise ValueError("テーマカラーは #RRGGBB 形式で入力してください。")

    port = int(payload.get("port", current.get("port", 8765)))
    if not 1024 <= port <= 65535:
        raise ValueError("ポート番号は1024から65535で入力してください。")

    config = {
        "app_name": "VTuber Analytics",
        "powered_by": "Aino Maria",
        "channel_name": channel_name,
        "owner_channel_ids": owner_ids,
        "chat_data_dir": chat_data_dir,
        "theme_color": theme_color,
        "host": "127.0.0.1",
        "port": port,
    }

    APP_CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    Path(chat_data_dir).expanduser().mkdir(parents=True, exist_ok=True)
    return {"ok": True, "config": public_app_config()}

class Handler(BaseHTTPRequestHandler):
    def send_json(self, value: Any, status: int = 200) -> None:
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_csv(self, body: bytes, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{filename}"'
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def send_html(self) -> None:
        body = INDEX_PATH.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        try:
            if path == "/":
                self.send_html()
            elif path == "/api/app-info":
                self.send_json(get_app_info())
            elif path == "/api/settings":
                self.send_json(public_app_config())
            elif path == "/api/summary":
                self.send_json(get_summary())
            elif path == "/api/categories":
                self.send_json(get_categories())
            elif path == "/api/weekdays":
                self.send_json(get_weekdays())
            elif path == "/api/community":
                self.send_json(get_community_analysis())
            elif path == "/api/moderation":
                query = params.get("q", [""])[0]
                status_filter = params.get("status", [""])[0]
                category_filter = params.get("category", [""])[0]
                limit = int(params.get("limit", ["200"])[0])
                self.send_json(get_moderation_candidates(
                    query=query,
                    status_filter=status_filter,
                    category_filter=category_filter,
                    limit=max(1, min(limit, 1000)),
                ))
            elif path.startswith("/api/moderation/context/"):
                message_id = urllib.parse.unquote(
                    path.removeprefix("/api/moderation/context/")
                )
                data = get_message_context(message_id)
                self.send_json(
                    data if data else {"error": "not found"},
                    200 if data else 404,
                )
            elif path == "/api/moderation/export.csv":
                self.send_csv(
                    export_moderation_csv(),
                    "moderation_reviews.csv",
                )
            elif path == "/api/streams":
                limit = int(params.get("limit", ["30"])[0])
                self.send_json(get_recent_streams(max(1, min(limit, 200))))
            elif path.startswith("/api/stream/"):
                video_id = urllib.parse.unquote(path.removeprefix("/api/stream/"))
                data = get_stream(video_id)
                self.send_json(data if data else {"error": "not found"}, 200 if data else 404)
            elif path == "/api/listeners":
                query = params.get("q", [""])[0].strip()
                self.send_json(search_listeners(query))
            elif path.startswith("/api/listener/"):
                channel_id = urllib.parse.unquote(path.removeprefix("/api/listener/"))
                data = get_listener(channel_id)
                self.send_json(data if data else {"error": "not found"}, 200 if data else 404)
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/moderation/review":
                payload = self.read_json_body()
                result = save_moderation_review(
                    message_id=str(payload.get("message_id", "")),
                    status=str(payload.get("status", "未確認")),
                    category=str(payload.get("category", "")),
                    memo=str(payload.get("memo", "")),
                )
                self.send_json(result)
            elif path == "/api/settings":
                payload = self.read_json_body()
                self.send_json(save_app_config(payload))
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[Web] {self.address_string()} - {fmt % args}")

def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")
    if not INDEX_PATH.exists():
        raise SystemExit(f"index.html not found: {INDEX_PATH}")

    ensure_moderation_storage()

    config = load_app_config()
    host = str(config.get("host", HOST))
    port = int(config.get("port", PORT))

    server = ThreadingHTTPServer((host, port), Handler)
    print("VTuber Analytics Web App v1.0.1")
    print(f"Open: http://{host}:{port}")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
