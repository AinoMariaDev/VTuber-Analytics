from __future__ import annotations

from pathlib import Path

from common import CHAT_DIR, connect, extract_messages, load_json_lines, parse_filename
from schema import initialize

def main() -> None:
    initialize()

    if not CHAT_DIR.exists():
        print(f"チャットフォルダが見つかりません: {CHAT_DIR}")
        print("このプロジェクトフォルダを、youtube_chat_dataと同じ親フォルダへ置いてください。")
        return

    files = sorted(CHAT_DIR.glob("*.live_chat.json"))
    if not files:
        print(f"チャットJSONがありません: {CHAT_DIR}")
        return

    imported_streams = 0
    imported_messages = 0
    skipped_files = 0

    with connect() as conn:
        for path in files:
            meta = parse_filename(path)
            video_id = meta["video_id"]

            exists = conn.execute(
                "SELECT 1 FROM streams WHERE source_file = ?",
                (path.name,),
            ).fetchone()
            if exists:
                skipped_files += 1
                continue

            conn.execute(
                """
                INSERT OR IGNORE INTO streams(video_id, stream_date, title, source_file)
                VALUES (?, ?, ?, ?)
                """,
                (video_id, meta["date"] or None, meta["title"], path.name),
            )

            file_message_count = 0

            for obj in load_json_lines(path):
                for message in extract_messages(obj):
                    channel_id = message["channel_id"]
                    display_name = message["display_name"]
                    stream_date = meta["date"] or None

                    conn.execute(
                        """
                        INSERT INTO listeners(
                            channel_id, latest_display_name,
                            first_seen_date, last_seen_date
                        )
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(channel_id) DO UPDATE SET
                            latest_display_name = excluded.latest_display_name,
                            first_seen_date = CASE
                                WHEN listeners.first_seen_date IS NULL THEN excluded.first_seen_date
                                WHEN excluded.first_seen_date IS NULL THEN listeners.first_seen_date
                                WHEN excluded.first_seen_date < listeners.first_seen_date THEN excluded.first_seen_date
                                ELSE listeners.first_seen_date
                            END,
                            last_seen_date = CASE
                                WHEN listeners.last_seen_date IS NULL THEN excluded.last_seen_date
                                WHEN excluded.last_seen_date IS NULL THEN listeners.last_seen_date
                                WHEN excluded.last_seen_date > listeners.last_seen_date THEN excluded.last_seen_date
                                ELSE listeners.last_seen_date
                            END,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (channel_id, display_name, stream_date, stream_date),
                    )

                    conn.execute(
                        """
                        INSERT INTO listener_names(
                            channel_id, display_name, first_seen_date, last_seen_date
                        )
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(channel_id, display_name) DO UPDATE SET
                            first_seen_date = CASE
                                WHEN listener_names.first_seen_date IS NULL THEN excluded.first_seen_date
                                WHEN excluded.first_seen_date IS NULL THEN listener_names.first_seen_date
                                WHEN excluded.first_seen_date < listener_names.first_seen_date THEN excluded.first_seen_date
                                ELSE listener_names.first_seen_date
                            END,
                            last_seen_date = CASE
                                WHEN listener_names.last_seen_date IS NULL THEN excluded.last_seen_date
                                WHEN excluded.last_seen_date IS NULL THEN listener_names.last_seen_date
                                WHEN excluded.last_seen_date > listener_names.last_seen_date THEN excluded.last_seen_date
                                ELSE listener_names.last_seen_date
                            END
                        """,
                        (channel_id, display_name, stream_date, stream_date),
                    )

                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO messages(
                            message_id, video_id, channel_id, display_name,
                            message_text, timestamp_usec, timestamp_text
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            message["message_id"],
                            video_id,
                            channel_id,
                            display_name,
                            message["message"],
                            message["timestamp_usec"],
                            message["timestamp_text"],
                        ),
                    )
                    if cursor.rowcount:
                        imported_messages += 1
                        file_message_count += 1

            imported_streams += 1
            print(f"[取込] {path.name} : {file_message_count}件")

        conn.commit()

    print()
    print("データベース構築が完了しました。")
    print(f"新規配信: {imported_streams}")
    print(f"新規コメント: {imported_messages}")
    print(f"取込済みでスキップ: {skipped_files}")
    print("保存先: data\\vtuber_analytics.db")

if __name__ == "__main__":
    main()
