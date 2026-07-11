from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from common import REPORT_DIR, connect, load_config

def write_csv(path: Path, headers: list[str], rows) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

def main() -> None:
    config = load_config()
    owner_ids = set(config.get("owner_channel_ids", []))
    inactive_days = int(config.get("inactive_days", 30))
    REPORT_DIR.mkdir(exist_ok=True)

    placeholders = ",".join("?" for _ in owner_ids) or "''"
    owner_params = tuple(owner_ids)

    with connect() as conn:
        latest_date_row = conn.execute(
            "SELECT MAX(stream_date) AS latest_date FROM streams"
        ).fetchone()
        latest_date = latest_date_row["latest_date"]

        listener_sql = f"""
        SELECT
            l.latest_display_name,
            l.channel_id,
            COUNT(DISTINCT m.video_id) AS stream_count,
            COUNT(m.message_id) AS comment_count,
            l.first_seen_date,
            l.last_seen_date,
            ROUND(
                CAST(COUNT(m.message_id) AS REAL) /
                NULLIF(COUNT(DISTINCT m.video_id), 0),
                1
            ) AS avg_comments
        FROM listeners l
        JOIN messages m ON m.channel_id = l.channel_id
        WHERE l.channel_id NOT IN ({placeholders})
        GROUP BY l.channel_id
        ORDER BY stream_count DESC, comment_count DESC
        """
        listener_rows = conn.execute(listener_sql, owner_params).fetchall()

        write_csv(
            REPORT_DIR / "リスナー一覧.csv",
            [
                "表示名", "チャンネルID", "参加配信数", "総コメント数",
                "平均コメント数", "初参加日", "最終参加日"
            ],
            [
                (
                    r["latest_display_name"], r["channel_id"],
                    r["stream_count"], r["comment_count"],
                    r["avg_comments"], r["first_seen_date"], r["last_seen_date"]
                )
                for r in listener_rows
            ],
        )

        stream_sql = f"""
        SELECT
            s.stream_date,
            s.video_id,
            s.title,
            COUNT(DISTINCT CASE
                WHEN m.channel_id NOT IN ({placeholders}) THEN m.channel_id
            END) AS participant_count,
            COUNT(CASE
                WHEN m.channel_id NOT IN ({placeholders}) THEN m.message_id
            END) AS comment_count
        FROM streams s
        LEFT JOIN messages m ON m.video_id = s.video_id
        GROUP BY s.video_id
        ORDER BY s.stream_date, s.video_id
        """
        stream_rows = conn.execute(stream_sql, owner_params * 2).fetchall()

        write_csv(
            REPORT_DIR / "配信一覧.csv",
            ["配信日", "動画ID", "タイトル", "コメント参加人数", "総コメント数"],
            [
                (
                    r["stream_date"], r["video_id"], r["title"],
                    r["participant_count"], r["comment_count"]
                )
                for r in stream_rows
            ],
        )

        if latest_date:
            inactive_sql = f"""
            SELECT
                l.latest_display_name,
                l.channel_id,
                COUNT(DISTINCT m.video_id) AS stream_count,
                COUNT(m.message_id) AS comment_count,
                l.first_seen_date,
                l.last_seen_date,
                CAST(julianday(?) - julianday(l.last_seen_date) AS INTEGER) AS days_absent
            FROM listeners l
            JOIN messages m ON m.channel_id = l.channel_id
            WHERE l.channel_id NOT IN ({placeholders})
            GROUP BY l.channel_id
            HAVING days_absent >= ?
            ORDER BY days_absent DESC, stream_count DESC
            """
            inactive_rows = conn.execute(
                inactive_sql,
                (latest_date, *owner_params, inactive_days),
            ).fetchall()
        else:
            inactive_rows = []

        write_csv(
            REPORT_DIR / f"{inactive_days}日以上コメントなし.csv",
            [
                "表示名", "チャンネルID", "参加配信数", "総コメント数",
                "初参加日", "最終参加日", "最終配信からの日数"
            ],
            [
                (
                    r["latest_display_name"], r["channel_id"],
                    r["stream_count"], r["comment_count"],
                    r["first_seen_date"], r["last_seen_date"], r["days_absent"]
                )
                for r in inactive_rows
            ],
        )

        latest_stream = conn.execute(
            """
            SELECT video_id, stream_date, title
            FROM streams
            WHERE stream_date IS NOT NULL
            ORDER BY stream_date DESC, imported_at DESC
            LIMIT 1
            """
        ).fetchone()

        report_lines = [
            f"{config.get('channel_name', 'VTuber')} 最新配信レポート",
            "=" * 40,
        ]

        if latest_stream:
            stats_sql = f"""
            SELECT
                COUNT(DISTINCT CASE
                    WHEN channel_id NOT IN ({placeholders}) THEN channel_id
                END) AS participants,
                COUNT(CASE
                    WHEN channel_id NOT IN ({placeholders}) THEN message_id
                END) AS comments
            FROM messages
            WHERE video_id = ?
            """
            stats = conn.execute(
                stats_sql,
                (*owner_params, *owner_params, latest_stream["video_id"]),
            ).fetchone()

            new_sql = f"""
            SELECT COUNT(*) AS new_count
            FROM listeners
            WHERE first_seen_date = ?
              AND channel_id NOT IN ({placeholders})
            """
            new_count = conn.execute(
                new_sql,
                (latest_stream["stream_date"], *owner_params),
            ).fetchone()["new_count"]

            report_lines.extend([
                f"配信日: {latest_stream['stream_date']}",
                f"タイトル: {latest_stream['title']}",
                f"コメント参加人数: {stats['participants']}",
                f"総コメント数: {stats['comments']}",
                f"その日が初参加のリスナー: {new_count}",
                "",
                f"データ基準日: {latest_date}",
            ])
        else:
            report_lines.append("配信データがありません。")

        (REPORT_DIR / "最新配信レポート.txt").write_text(
            "\n".join(report_lines),
            encoding="utf-8",
        )

    print("分析レポートを作成しました。")
    print(f"出力先: {REPORT_DIR}")
    print("・リスナー一覧.csv")
    print("・配信一覧.csv")
    print(f"・{inactive_days}日以上コメントなし.csv")
    print("・最新配信レポート.txt")

if __name__ == "__main__":
    main()
