from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterator

PROJECT_DIR = Path(__file__).resolve().parent.parent
PARENT_DIR = PROJECT_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
REPORT_DIR = PROJECT_DIR / "reports"
DB_PATH = DATA_DIR / "vtuber_analytics.db"
CONFIG_PATH = PROJECT_DIR / "config.json"
APP_CONFIG_PATH = PROJECT_DIR / "app_config.local.json"

def resolve_chat_dir() -> Path:
    candidates: list[Path] = []
    if APP_CONFIG_PATH.exists():
        try:
            with APP_CONFIG_PATH.open("r", encoding="utf-8") as f:
                app_config = json.load(f)
            configured = str(app_config.get("chat_data_dir", "")).strip()
            if configured:
                candidates.append(Path(configured).expanduser())
        except Exception:
            pass

    candidates.extend([
        PROJECT_DIR / "youtube_chat_data",
        PARENT_DIR / "youtube_chat_data",
    ])

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]

CHAT_DIR = resolve_chat_dir()

FILE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<video_id>[^_]+)_(?P<title>.*)\.live_chat\.json$"
)

def load_config() -> dict[str, Any]:
    """Load current local app settings, with legacy config fallback."""
    if APP_CONFIG_PATH.exists():
        try:
            with APP_CONFIG_PATH.open("r", encoding="utf-8") as f:
                app_config = json.load(f)
            return {
                "channel_name": app_config.get("channel_name", "VTuber"),
                "owner_channel_ids": app_config.get("owner_channel_ids", []),
                "inactive_days": int(app_config.get("inactive_days", 30)),
            }
        except Exception:
            pass

    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)

    return {
        "channel_name": "VTuber",
        "owner_channel_ids": [],
        "inactive_days": 30,
    }

def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn

def parse_filename(path: Path) -> dict[str, str]:
    match = FILE_RE.match(path.name)
    if match:
        return match.groupdict()
    return {
        "date": "",
        "video_id": path.stem.replace(".live_chat", ""),
        "title": path.stem.replace(".live_chat", ""),
    }

def iter_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)

def text_value(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("simpleText"), str):
        return value["simpleText"].strip()
    runs = value.get("runs")
    if not isinstance(runs, list):
        return ""
    parts = []
    for run in runs:
        if isinstance(run, dict) and isinstance(run.get("text"), str):
            parts.append(run["text"])
    return "".join(parts).strip()

def message_value(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    runs = value.get("runs")
    if not isinstance(runs, list):
        return ""
    parts: list[str] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        if isinstance(run.get("text"), str):
            parts.append(run["text"])
            continue
        emoji = run.get("emoji")
        if isinstance(emoji, dict):
            label = (
                emoji.get("image", {})
                .get("accessibility", {})
                .get("accessibilityData", {})
                .get("label")
            )
            if isinstance(label, str):
                parts.append(label)
    return "".join(parts).strip()

def load_json_lines(path: Path) -> Iterator[Any]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        first = True
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
                first = False
            except json.JSONDecodeError:
                if first:
                    f.seek(0)
                    try:
                        yield json.load(f)
                    except Exception:
                        pass
                return

def extract_messages(obj: Any) -> Iterator[dict[str, str]]:
    seen: set[str] = set()
    for item in iter_dicts(obj):
        channel_id = item.get("authorExternalChannelId")
        display_name = text_value(item.get("authorName"))
        if not channel_id or not display_name:
            continue

        message_id = str(item.get("id", ""))
        timestamp_usec = str(item.get("timestampUsec", ""))
        message = message_value(item.get("message"))
        key = message_id or f"{channel_id}:{timestamp_usec}:{message}"

        if key in seen:
            continue
        seen.add(key)

        yield {
            "message_id": key,
            "channel_id": str(channel_id),
            "display_name": display_name,
            "message": message,
            "timestamp_usec": timestamp_usec,
            "video_offset_msec": str(item.get("_videoOffsetTimeMsec", "")),
            "timestamp_text": text_value(item.get("timestampText")),
        }
