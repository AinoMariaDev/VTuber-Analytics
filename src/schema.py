from __future__ import annotations
from common import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
    video_id TEXT PRIMARY KEY,
    stream_date TEXT,
    title TEXT NOT NULL,
    source_file TEXT NOT NULL UNIQUE,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS listeners (
    channel_id TEXT PRIMARY KEY,
    latest_display_name TEXT NOT NULL,
    first_seen_date TEXT,
    last_seen_date TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS listener_names (
    channel_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    first_seen_date TEXT,
    last_seen_date TEXT,
    PRIMARY KEY (channel_id, display_name),
    FOREIGN KEY (channel_id) REFERENCES listeners(channel_id)
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    message_text TEXT,
    timestamp_usec TEXT,
    timestamp_text TEXT,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (video_id) REFERENCES streams(video_id),
    FOREIGN KEY (channel_id) REFERENCES listeners(channel_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_video ON messages(video_id);
CREATE INDEX IF NOT EXISTS idx_messages_listener ON messages(channel_id);
CREATE INDEX IF NOT EXISTS idx_streams_date ON streams(stream_date);
"""

def initialize() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)

if __name__ == "__main__":
    initialize()
    print("データベースを初期化しました。")
