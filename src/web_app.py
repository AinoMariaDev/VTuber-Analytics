from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import urllib.parse
import mimetypes
import os
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from weekly_schedule import load_week, save_week
from deployment_bootstrap import bootstrap_persistent_storage
from recovery_audit import (
    cleanup_old_backups,
    record_audit,
    recovery_status,
    save_recovery_config,
    verify_all_backups,
    verify_backup_by_name,
)
from background_jobs import BACKGROUND_JOB_MANAGER, load_config as load_background_config, save_config as save_background_config
from youtube_sync import (
    connection_status as youtube_connection_status,
    create_authorization_url as youtube_authorization_url,
    exchange_code as youtube_exchange_code,
    save_settings as save_youtube_settings,
    sync_live_streams,
)
from auth import (
    authenticate,
    clear_session_cookie,
    create_initial_admin,
    create_session,
    current_user,
    destroy_session,
    role_at_least,
    session_cookie,
    setup_required,
)
from full_backup import create_backup, list_backups, restore_backup
from storage_paths import BACKUP_DIR, CHAT_DIR, CONFIG_PATH as APP_CONFIG_PATH, DB_PATH, LOG_DIR, MODERATION_RULES_PATH, PROJECT_DIR, WEEKLY_SCHEDULE_DIR, ensure_storage_directories, storage_summary
from stream_metadata import (
    category_analysis,
    ensure_stream_metadata_schema,
    metadata_status,
    reclassify_all_streams,
    stream_tag_map,
)

INDEX_PATH = PROJECT_DIR / "web" / "index.html"
HOST = "0.0.0.0"
PORT = 8765

WEB_ERROR_HISTORY: deque[dict[str, str]] = deque(maxlen=20)
WEB_ERROR_LOCK = threading.Lock()


def record_web_error(path: str, exc: Exception) -> None:
    with WEB_ERROR_LOCK:
        WEB_ERROR_HISTORY.appendleft({
            "occurred_at": datetime.now().isoformat(timespec="seconds"),
            "path": path,
            "type": type(exc).__name__,
            "message": str(exc),
        })


def readable_size(size_bytes: int) -> str:
    value = float(max(0, size_bytes))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def command_version(command: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=8,
            cwd=str(PROJECT_DIR),
        )
        output = (result.stdout or result.stderr or "").strip().splitlines()
        return {
            "available": result.returncode == 0,
            "version": output[0] if output else "",
            "error": "" if result.returncode == 0 else f"終了コード {result.returncode}",
        }
    except FileNotFoundError:
        return {"available": False, "version": "", "error": "見つかりません"}
    except subprocess.TimeoutExpired:
        return {"available": False, "version": "", "error": "確認がタイムアウトしました"}
    except OSError as exc:
        return {"available": False, "version": "", "error": str(exc)}


def database_diagnostics() -> dict[str, Any]:
    result: dict[str, Any] = {
        "exists": DB_PATH.exists(),
        "path": str(DB_PATH),
        "size_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
        "size_text": readable_size(DB_PATH.stat().st_size) if DB_PATH.exists() else "0 B",
        "integrity": "未確認",
        "stream_count": 0,
        "listener_count": 0,
        "error": "",
    }
    if not DB_PATH.exists():
        result["integrity"] = "DBがありません"
        return result

    try:
        connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        try:
            integrity_row = connection.execute("PRAGMA quick_check").fetchone()
            result["integrity"] = str(integrity_row[0] if integrity_row else "不明")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "streams" in tables:
                result["stream_count"] = int(
                    connection.execute("SELECT COUNT(*) FROM streams").fetchone()[0]
                )
            if "listeners" in tables:
                result["listener_count"] = int(
                    connection.execute("SELECT COUNT(*) FROM listeners").fetchone()[0]
                )
            elif "listener_summary" in tables:
                result["listener_count"] = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM listener_summary"
                    ).fetchone()[0]
                )
        finally:
            connection.close()
    except (sqlite3.Error, OSError) as exc:
        result["integrity"] = "エラー"
        result["error"] = str(exc)
    return result


def get_diagnostics() -> dict[str, Any]:
    config = load_app_config()
    chat_dir = Path(str(config.get("chat_data_dir", CHAT_DIR)))
    log_candidates = [
        PROJECT_DIR / "logs",
        LOG_DIR,
        PROJECT_DIR / "web_app.log",
    ]
    existing_logs = [
        str(path) for path in log_candidates if path.exists()
    ]

    with WEB_ERROR_LOCK:
        errors = list(WEB_ERROR_HISTORY)

    return {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "application": {
            "project_dir": str(PROJECT_DIR),
            "index_exists": INDEX_PATH.exists(),
            "config_exists": APP_CONFIG_PATH.exists(),
            "moderation_rules_exists": (PROJECT_DIR / "moderation_rules.local.json").exists(),
        },
        "database": database_diagnostics(),
        "runtime": {
            "python": {
                "available": True,
                "version": sys.version.split()[0],
                "executable": sys.executable,
            },
            "yt_dlp": command_version([sys.executable, "-m", "yt_dlp", "--version"]),
        },
        "storage": {
            "chat_data_dir": str(chat_dir),
            "chat_data_dir_exists": chat_dir.exists(),
            "chat_json_count": (
                len(list(chat_dir.glob("*.json"))) if chat_dir.exists() else 0
            ),
            "weekly_schedule_dir": str(WEEKLY_SCHEDULE_DIR),
            "weekly_schedule_dir_exists": (
                WEEKLY_SCHEDULE_DIR
            ).exists(),
            "backup_dir": str(BACKUP_DIR),
            "backup_dir_exists": (BACKUP_DIR).exists(),
            "log_locations": existing_logs,
        },
        "chat_download": CHAT_DOWNLOAD_MANAGER.snapshot(),
        "recent_errors": errors,
        "storage_paths": storage_summary(),
    }

APP_CONFIG_EXAMPLE_PATH = PROJECT_DIR / "app_config.example.json"

DEFAULT_APP_CONFIG = {
    "app_name": "VTuber Analytics",
    "powered_by": "Aino Maria",
    "channel_name": "Aino Maria",
    "owner_channel_ids": ["UCbPtcsXkPLLiOySGZJW92gw"],
    "chat_data_dir": str(CHAT_DIR),
    "theme_color": "#24324A",
    "host": "127.0.0.1",
    "port": 8765
}


class ChatDownloadManager:
    """Web画面から実行するyt-dlpチャット取得の進捗を管理する。"""

    ITEM_PREFIX = "__VTA_ITEM__"
    DONE_PREFIX = "__VTA_DONE__"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = self._empty_state()

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "running": False,
            "status": "idle",
            "message": "待機中",
            "url": "",
            "total": 0,
            "current": 0,
            "success": 0,
            "failed": 0,
            "current_title": "",
            "current_video_id": "",
            "started_at": "",
            "finished_at": "",
            "elapsed_seconds": 0,
            "errors": [],
            "log_tail": [],
            "cancel_requested": False,
            "return_code": None,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            value = dict(self._state)
            if value["running"] and value["started_at"]:
                try:
                    started = datetime.fromisoformat(value["started_at"])
                    value["elapsed_seconds"] = max(
                        0, int((datetime.now() - started).total_seconds())
                    )
                except ValueError:
                    pass
            value["errors"] = list(value["errors"])
            value["log_tail"] = list(value["log_tail"])
            return value

    @staticmethod
    def validate_url(url: str) -> tuple[bool, str]:
        value = url.strip()
        if not value:
            return False, "YouTubeのライブ一覧URLを入力してください。"
        lowered = value.lower()
        if "studio.youtube.com" in lowered:
            return False, "YouTube Studioではなく、公開チャンネルのライブタブURLを入力してください。"
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            return False, "http または https から始まるURLを入力してください。"
        hostname = (parsed.hostname or "").lower()
        if hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
            return False, "YouTubeのURLを入力してください。"
        return True, ""

    def start(self, url: str) -> dict[str, Any]:
        valid, message = self.validate_url(url)
        if not valid:
            raise ValueError(message)

        with self._lock:
            if self._state["running"]:
                raise RuntimeError("チャット取得はすでに実行中です。")
            self._state = self._empty_state()
            self._state.update({
                "running": True,
                "status": "starting",
                "message": "yt-dlpを起動しています...",
                "url": url.strip(),
                "started_at": datetime.now().isoformat(timespec="seconds"),
            })
            self._thread = threading.Thread(
                target=self._run,
                args=(url.strip(),),
                daemon=True,
                name="vta-chat-download",
            )
            self._thread.start()

        return self.snapshot()

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            if not self._state["running"]:
                return self.snapshot()
            self._state["cancel_requested"] = True
            self._state["status"] = "cancelling"
            self._state["message"] = "取得処理を中止しています..."
            process = self._process

        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        return self.snapshot()

    def _append_log(self, line: str) -> None:
        clean = self._safe_log_line(line)
        if not clean:
            return
        with self._lock:
            tail = self._state["log_tail"]
            tail.append(clean)
            del tail[:-30]

    def _append_error(self, line: str) -> None:
        clean = self._safe_log_line(line)
        with self._lock:
            errors = self._state["errors"]
            if clean and clean not in errors:
                errors.append(clean)
                del errors[:-20]

    @staticmethod
    def _title_from_video_id(video_id: str) -> str:
        if not video_id:
            return "配信情報を確認中"
        try:
            with sqlite3.connect(DB_PATH) as connection:
                row = connection.execute(
                    "SELECT title FROM streams WHERE video_id = ? LIMIT 1",
                    (video_id,),
                ).fetchone()
            if row and row[0]:
                return str(row[0])
        except sqlite3.Error:
            pass
        return f"動画ID: {video_id}"

    @staticmethod
    def _safe_log_line(value: str) -> str:
        """壊れた日本語や制御文字をログから除外する。"""
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
        text = "".join(
            char for char in text
            if char in "\t " or 32 <= ord(char) <= 126
        ).strip()
        return text

    def _update_item(self, payload: str) -> None:
        parts = payload.split("|", 2)
        while len(parts) < 3:
            parts.append("")
        index_text, total_text, video_id = parts
        try:
            index = int(index_text or 0)
        except ValueError:
            index = 0
        try:
            total = int(total_text or 0)
        except ValueError:
            total = 0

        with self._lock:
            if total > 0:
                self._state["total"] = max(self._state["total"], total)
            if index > 0:
                self._state["current"] = max(self._state["current"], index)
            self._state["current_video_id"] = video_id
            self._state["current_title"] = self._title_from_video_id(video_id)
            self._state["status"] = "running"
            if self._state["total"]:
                self._state["message"] = (
                    f'{self._state["current"]}/{self._state["total"]}件目を取得中'
                )
            else:
                self._state["message"] = "ライブチャットを取得中"

    def _mark_done(self, payload: str) -> None:
        parts = payload.split("|", 2)
        while len(parts) < 3:
            parts.append("")
        index_text, total_text, video_id = parts
        try:
            index = int(index_text or 0)
        except ValueError:
            index = 0
        try:
            total = int(total_text or 0)
        except ValueError:
            total = 0

        with self._lock:
            if total > 0:
                self._state["total"] = max(self._state["total"], total)
            if index > 0:
                self._state["current"] = max(self._state["current"], index)
            self._state["success"] += 1
            self._state["current_video_id"] = video_id
            self._state["current_title"] = self._title_from_video_id(video_id)
            if self._state["total"]:
                self._state["message"] = (
                    f'{self._state["success"]}/{self._state["total"]}件を処理しました'
                )

    def _run(self, url: str) -> None:
        config = load_app_config()
        out_dir = Path(
            str(config.get("chat_data_dir", CHAT_DIR))
        ).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(
            out_dir / "%(upload_date>%Y-%m-%d)s_%(id)s_%(title).120B.%(ext)s"
        )

        cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--skip-download",
            "--write-subs",
            "--sub-langs",
            "live_chat",
            "--ignore-errors",
            "--no-overwrites",
            "--windows-filenames",
            "--newline",
            "--no-color",
            "--print",
            f"video:{self.ITEM_PREFIX}%(playlist_index|0)s|%(playlist_count|0)s|%(id)s",
            "--print",
            f"after_video:{self.DONE_PREFIX}%(playlist_index|0)s|%(playlist_count|0)s|%(id)s",
            "--output",
            output_template,
            url,
        ]

        return_code = -1
        try:
            process = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            with self._lock:
                self._process = process
                self._state["status"] = "running"
                self._state["message"] = "YouTubeの配信一覧を確認しています..."

            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                self._append_log(line)

                if line.startswith(self.ITEM_PREFIX):
                    self._update_item(line[len(self.ITEM_PREFIX):])
                elif line.startswith(self.DONE_PREFIX):
                    self._mark_done(line[len(self.DONE_PREFIX):])
                elif "ERROR:" in line:
                    self._append_error(line)
                    with self._lock:
                        self._state["failed"] += 1

            return_code = process.wait()
        except Exception as exc:
            self._append_error(str(exc))
            with self._lock:
                self._state["failed"] += 1
        finally:
            with self._lock:
                cancelled = bool(self._state["cancel_requested"])
                self._process = None
                self._state["running"] = False
                self._state["return_code"] = return_code
                self._state["finished_at"] = datetime.now().isoformat(timespec="seconds")
                try:
                    started = datetime.fromisoformat(self._state["started_at"])
                    self._state["elapsed_seconds"] = max(
                        0, int((datetime.now() - started).total_seconds())
                    )
                except (ValueError, TypeError):
                    pass

                if cancelled:
                    self._state["status"] = "cancelled"
                    self._state["message"] = "チャット取得を中止しました。"
                elif return_code == 0:
                    self._state["status"] = "completed"
                    self._state["message"] = "チャット取得が完了しました。"
                else:
                    self._state["status"] = "failed"
                    self._state["message"] = "一部または全部のチャット取得に失敗しました。"


CHAT_DOWNLOAD_MANAGER = ChatDownloadManager()

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
    return category_analysis(OWNER_CHANNEL_IDS)

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
            SELECT s.stream_date, s.video_id, s.title,
                   COALESCE(s.category, 'その他') AS category,
                   COALESCE(s.weekday, '') AS weekday,
                   COALESCE(s.source_type, '不明') AS source_type,
                   COALESCE(s.last_synced_at, s.imported_at) AS last_synced_at,
                   COUNT(DISTINCT CASE WHEN m.channel_id NOT IN ({owner_ph}) THEN m.channel_id END) AS participants,
                   COUNT(CASE WHEN m.channel_id NOT IN ({owner_ph}) THEN m.message_id END) AS comments
            FROM streams s
            LEFT JOIN messages m ON m.video_id = s.video_id
            GROUP BY s.video_id
            ORDER BY s.stream_date DESC, s.imported_at DESC
            LIMIT ?
            """,
            (*owner_params, *owner_params, limit),
        ).fetchall()
    tag_map = stream_tag_map()
    result = []
    for row in rows:
        item = dict(row)
        item["tags"] = tag_map.get(str(row["video_id"]), [item["category"]])
        result.append(item)
    return result

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
            WHERE COALESCE(s.category, 'その他') = ?
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
        "version": "1.0.0",
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
            CHAT_DIR,
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
            current.get("chat_data_dir", CHAT_DIR),
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
    def auth_user(self):
        return current_user(self.headers.get("Cookie"))

    def send_json_with_cookie(
        self,
        value: Any,
        cookie_value: str,
        status: int = 200,
    ) -> None:
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", cookie_value)
        self.end_headers()
        self.wfile.write(body)

    def require_login(self):
        user = self.auth_user()
        if user is None:
            self.send_json(
                {
                    "error": "login required",
                    "code": "AUTH_REQUIRED",
                    "setup_required": setup_required(),
                },
                401,
            )
            return None
        return user

    def require_role(self, required_role: str):
        user = self.require_login()
        if user is None:
            return None
        if not role_at_least(user, required_role):
            self.send_json(
                {
                    "error": "この操作を行う権限がありません。",
                    "code": "FORBIDDEN",
                    "required_role": required_role,
                },
                403,
            )
            return None
        return user
    def send_json(self, value: Any, status: int = 200) -> None:
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, filename: str) -> None:
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{filename}"'
        )
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

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

    def send_html_text(self, html_text: str, status: int = 200) -> None:
        body = html_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
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
        path = parsed.path.rstrip("/") or "/"
        params = urllib.parse.parse_qs(parsed.query)

        public_get_paths = {
            "/api/auth/status",
            "/api/auth/setup-check",
            "/api/youtube/oauth/callback",
            "/health",
            "/api/health",
        }
        if (
            path.startswith("/api/")
            and path not in public_get_paths
            and self.auth_user() is None
        ):
            self.send_json(
                {
                    "error": "login required",
                    "code": "AUTH_REQUIRED",
                    "setup_required": setup_required(),
                },
                401,
            )
            return

        try:
            if path == "/":
                self.send_html()
            elif path in {"/health", "/api/health"}:
                database_ok = DB_PATH.exists()
                self.send_json({
                    "ok": database_ok,
                    "service": "VTuber Analytics",
                    "version": "1.0.0",
                    "database": database_ok,
                    "background_jobs": BACKGROUND_JOB_MANAGER.state().get("thread_alive", False),
                }, 200 if database_ok else 503)
            elif path == "/api/auth/setup-check":
                self.send_json({
                    "ok": True,
                    "setup_required": setup_required(),
                    "setup_endpoint": "/api/auth/setup",
                })
            elif path == "/api/auth/status":
                user = self.auth_user()
                self.send_json({
                    "authenticated": user is not None,
                    "setup_required": setup_required(),
                    "user": user.public() if user else None,
                    "session_ttl_seconds": 28800,
                })
            elif path == "/api/app-info":
                if self.require_login() is None:
                    return
                self.send_json(get_app_info())
            elif path == "/api/youtube/status":
                if self.require_role("admin") is None:
                    return
                self.send_json(youtube_connection_status())
            elif path == "/api/background-jobs/status":
                if self.require_role("admin") is None:
                    return
                self.send_json(BACKGROUND_JOB_MANAGER.state())
            elif path == "/api/recovery/status":
                if self.require_role("admin") is None:
                    return
                self.send_json(recovery_status())
            elif path == "/api/recovery/verify":
                if self.require_role("admin") is None:
                    return
                filename = params.get("filename", [""])[0]
                self.send_json(verify_backup_by_name(filename))
            elif path == "/api/recovery/verify-all":
                if self.require_role("admin") is None:
                    return
                self.send_json({"items": verify_all_backups(50)})
            elif path == "/api/youtube/oauth/start":
                if self.require_role("admin") is None:
                    return
                self.send_json({"authorization_url": youtube_authorization_url()})
            elif path == "/api/youtube/oauth/callback":
                error = params.get("error", [""])[0]
                if error:
                    self.send_html_text(
                        "<!doctype html><meta charset='utf-8'><title>YouTube連携エラー</title>"
                        "<h1>YouTube連携を完了できませんでした。</h1>"
                        f"<p>{error}</p><a href='/'>アプリへ戻る</a>",
                        400,
                    )
                    return
                channel = youtube_exchange_code(
                    params.get("code", [""])[0],
                    params.get("state", [""])[0],
                )
                self.send_html_text(
                    "<!doctype html><meta charset='utf-8'><title>YouTube連携完了</title>"
                    "<script>location.replace('/?youtube=connected');</script>"
                    f"<p>{channel.get('title', '')}との連携が完了しました。</p>"
                )
            elif path == "/api/settings":
                if self.require_role("admin") is None:
                    return
                if self.require_role("admin") is None:
                    return
                self.send_json(public_app_config())
            elif path == "/api/chat-download/status":
                if self.require_login() is None:
                    return
                self.send_json(CHAT_DOWNLOAD_MANAGER.snapshot())
            elif path == "/api/diagnostics":
                if self.require_role("admin") is None:
                    return
                self.send_json(get_diagnostics())
            elif path == "/api/summary":
                if self.require_login() is None:
                    return
                self.send_json(get_summary())
            elif path == "/api/backups":
                if self.require_role("admin") is None:
                    return
                self.send_json({"backups": list_backups()})
            elif path == "/api/backup/download":
                if self.require_role("admin") is None:
                    return
                include_chat = params.get("include_chat", ["1"])[0] != "0"
                backup = create_backup(include_chat_data=include_chat)
                self.send_file(backup["path"], backup["filename"])
            elif path == "/api/weekly-schedule":
                week_start = params.get("week_start", [""])[0]
                self.send_json(load_week(week_start or None))
            elif path == "/api/categories":
                self.send_json(get_categories())
            elif path == "/api/stream-metadata/status":
                self.send_json(metadata_status())
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
            record_web_error(path, exc)
            self.send_json({"error": str(exc)}, 500)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        public_auth_paths = {
            "/api/auth/setup",
            "/api/auth/initial-admin",
            "/api/setup",
            "/api/auth/login",
            "/api/auth/logout",
        }
        if path.startswith("/api/") and path not in public_auth_paths:
            if self.auth_user() is None:
                self.send_json(
                    {
                        "error": "login required",
                        "code": "AUTH_REQUIRED",
                        "setup_required": setup_required(),
                    },
                    401,
                )
                return

        try:
            if path in {
                "/api/auth/setup",
                "/api/auth/initial-admin",
                "/api/setup",
            }:
                payload = self.read_json_body()
                user = create_initial_admin(
                    username=str(payload.get("username", "")),
                    display_name=str(payload.get("display_name", "")),
                    password=str(payload.get("password", "")),
                )
                auth_user = authenticate(
                    str(payload.get("username", "")),
                    str(payload.get("password", "")),
                )
                if auth_user is None:
                    raise ValueError("管理者作成後の認証に失敗しました。")
                token = create_session(auth_user)
                self.send_json_with_cookie(
                    {"ok": True, "user": user},
                    session_cookie(token),
                    201,
                )
            elif path == "/api/auth/login":
                payload = self.read_json_body()
                user = authenticate(
                    str(payload.get("username", "")),
                    str(payload.get("password", "")),
                )
                if user is None:
                    self.send_json(
                        {"error": "ユーザー名またはパスワードが違います。"},
                        401,
                    )
                    return
                token = create_session(user)
                self.send_json_with_cookie(
                    {"ok": True, "user": user.public()},
                    session_cookie(token),
                )
            elif path == "/api/auth/logout":
                destroy_session(self.headers.get("Cookie"))
                self.send_json_with_cookie(
                    {"ok": True},
                    clear_session_cookie(),
                )
            elif path == "/api/moderation/review":
                if self.require_role("moderator") is None:
                    return
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
            elif path == "/api/youtube/settings":
                if self.require_role("admin") is None:
                    return
                payload = self.read_json_body()
                self.send_json(save_youtube_settings(
                    client_id=str(payload.get("client_id", "")),
                    client_secret=str(payload.get("client_secret", "")),
                    redirect_uri=str(payload.get("redirect_uri", "")),
                ))
            elif path == "/api/youtube/sync":
                if self.require_role("admin") is None:
                    return
                payload = self.read_json_body()
                result = sync_live_streams(
                    max_pages=int(payload.get("max_pages", 20))
                )
                record_audit(
                    "youtube_sync_manual",
                    status="success",
                    actor=self.auth_user().username,
                    role=self.auth_user().role,
                    details=result,
                )
                self.send_json(result)
            elif path == "/api/background-jobs/settings":
                if self.require_role("admin") is None:
                    return
                payload = self.read_json_body()
                saved = save_background_config(payload)
                BACKGROUND_JOB_MANAGER.wake()
                self.send_json(saved)
            elif path == "/api/background-jobs/run":
                if self.require_role("admin") is None:
                    return
                payload = self.read_json_body()
                self.send_json(
                    BACKGROUND_JOB_MANAGER.run_now(
                        str(payload.get("job", ""))
                    ),
                    202,
                )
            elif path == "/api/recovery/settings":
                user = self.require_role("admin")
                if user is None:
                    return
                payload = self.read_json_body()
                saved = save_recovery_config(payload)
                record_audit(
                    "recovery_settings",
                    status="success",
                    actor=user.username,
                    role=user.role,
                    details=saved,
                )
                self.send_json(saved)
            elif path == "/api/recovery/cleanup":
                user = self.require_role("admin")
                if user is None:
                    return
                result = cleanup_old_backups()
                record_audit(
                    "backup_cleanup",
                    status="success",
                    actor=user.username,
                    role=user.role,
                    details=result,
                )
                self.send_json(result)
            elif path == "/api/chat-download/start":
                if self.require_role("admin") is None:
                    return
                payload = self.read_json_body()
                self.send_json(
                    CHAT_DOWNLOAD_MANAGER.start(str(payload.get("url", ""))),
                    202,
                )
            elif path == "/api/chat-download/cancel":
                if self.require_role("admin") is None:
                    return
                self.send_json(CHAT_DOWNLOAD_MANAGER.cancel())
            elif path == "/api/stream-metadata/reclassify":
                if self.require_role("admin") is None:
                    return
                payload = self.read_json_body()
                result = reclassify_all_streams(
                    recompute_weekday=bool(payload.get("recompute_weekday", True))
                )
                record_audit(
                    "reclassify_manual",
                    status="success",
                    actor=self.auth_user().username,
                    role=self.auth_user().role,
                    details=result,
                )
                self.send_json(result)
            elif path == "/api/backup/restore":
                if self.require_role("admin") is None:
                    return
                content_length = int(self.headers.get("Content-Length", "0"))
                self.send_json(restore_backup(self.rfile, content_length))
            elif path == "/api/weekly-schedule":
                if self.require_role("moderator") is None:
                    return
                payload = self.read_json_body()
                self.send_json(save_week(payload))
            else:
                self.send_json({
                    "error": "API endpoint not found",
                    "code": "API_NOT_FOUND",
                    "path": path,
                    "method": "POST",
                }, 404)
        except Exception as exc:
            record_web_error(path, exc)
            self.send_json({"error": str(exc)}, 400)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[Web] {self.address_string()} - {fmt % args}")

def main() -> None:
    if not INDEX_PATH.exists():
        raise SystemExit(f"index.html not found: {INDEX_PATH}")

    bootstrap_result = bootstrap_persistent_storage()
    print(f"[Bootstrap] {bootstrap_result}")

    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")

    ensure_storage_directories()
    ensure_moderation_storage()
    ensure_stream_metadata_schema()
    BACKGROUND_JOB_MANAGER.start()

    config = load_app_config()
    server_mode = os.environ.get("VTA_SERVER_MODE") == "1"
    host = os.environ.get("HOST", "0.0.0.0" if server_mode else str(config.get("host", HOST)))
    port = int(os.environ.get("PORT", str(config.get("port", PORT))))

    server = ThreadingHTTPServer((host, port), Handler)
    print("VTuber Analytics Web App v1.0.0")
    print(f"Listening: http://{host}:{port}")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        BACKGROUND_JOB_MANAGER.stop()
        server.server_close()

if __name__ == "__main__":
    main()
