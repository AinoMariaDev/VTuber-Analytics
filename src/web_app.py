from __future__ import annotations

import json
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
OWNER_CHANNEL_IDS = {"UCbPtcsXkPLLiOySGZJW92gw"}

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
                COALESCE(s.category, 'その他') AS category,
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
            GROUP BY COALESCE(s.category, 'その他')
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
                COALESCE(s.category, 'その他') AS category,
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

def get_stream(video_id: str) -> dict[str, Any] | None:
    owner_ph = placeholders(OWNER_CHANNEL_IDS)
    owner_params = tuple(OWNER_CHANNEL_IDS)

    with connect() as conn:
        stream = conn.execute(
            """
            SELECT video_id, stream_date, title,
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

    return {
        "stream": dict(stream),
        "stats": dict(stats),
        "top_commenters": [dict(r) for r in top],
        "new_listeners": [dict(r) for r in new_people],
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
                COALESCE(s.category, 'その他') AS category,
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

class Handler(BaseHTTPRequestHandler):
    def send_json(self, value: Any, status: int = 200) -> None:
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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
            elif path == "/api/summary":
                self.send_json(get_summary())
            elif path == "/api/categories":
                self.send_json(get_categories())
            elif path == "/api/weekdays":
                self.send_json(get_weekdays())
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

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[Web] {self.address_string()} - {fmt % args}")

def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")
    if not INDEX_PATH.exists():
        raise SystemExit(f"index.html not found: {INDEX_PATH}")

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("VTuber Analytics Web App v0.6.2")
    print(f"Open: http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
