from __future__ import annotations

import json
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from full_backup import create_backup
from storage_paths import BACKGROUND_JOBS_CONFIG_PATH, BACKGROUND_JOBS_HISTORY_PATH
from stream_metadata import reclassify_all_streams
from time_utils import now_jst
from youtube_sync import connection_status, sync_live_streams
from recovery_audit import record_audit


DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "youtube_sync_enabled": True,
    "youtube_sync_interval_minutes": 180,
    "reclassify_enabled": True,
    "reclassify_interval_minutes": 360,
    "backup_enabled": True,
    "backup_interval_minutes": 1440,
    "backup_include_chat_data": False,
    "history_limit": 100,
}

_LOCK = threading.RLock()


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def load_config() -> dict[str, Any]:
    data = _read_json(BACKGROUND_JOBS_CONFIG_PATH, {})
    merged = dict(DEFAULT_CONFIG)
    if isinstance(data, dict):
        merged.update(data)
    return merged


def save_config(payload: dict[str, Any]) -> dict[str, Any]:
    config = load_config()
    boolean_keys = (
        "enabled",
        "youtube_sync_enabled",
        "reclassify_enabled",
        "backup_enabled",
        "backup_include_chat_data",
    )
    for key in boolean_keys:
        if key in payload:
            config[key] = bool(payload[key])

    interval_keys = (
        "youtube_sync_interval_minutes",
        "reclassify_interval_minutes",
        "backup_interval_minutes",
    )
    for key in interval_keys:
        if key in payload:
            value = int(payload[key])
            if value < 15:
                raise ValueError("定期処理の間隔は15分以上にしてください。")
            if value > 60 * 24 * 30:
                raise ValueError("定期処理の間隔が長すぎます。")
            config[key] = value

    if "history_limit" in payload:
        config["history_limit"] = max(20, min(int(payload["history_limit"]), 1000))

    _write_json(BACKGROUND_JOBS_CONFIG_PATH, config)
    return config


def _history() -> list[dict[str, Any]]:
    data = _read_json(BACKGROUND_JOBS_HISTORY_PATH, [])
    return data if isinstance(data, list) else []


def _append_history(item: dict[str, Any]) -> None:
    with _LOCK:
        config = load_config()
        history = _history()
        history.insert(0, item)
        history = history[: int(config.get("history_limit", 100))]
        _write_json(BACKGROUND_JOBS_HISTORY_PATH, history)


def recent_history(limit: int = 30) -> list[dict[str, Any]]:
    return _history()[: max(1, min(limit, 200))]


def _last_success(job_name: str) -> datetime | None:
    for item in _history():
        if item.get("job") == job_name and item.get("status") == "success":
            try:
                return datetime.fromisoformat(str(item["finished_at"]))
            except (KeyError, ValueError, TypeError):
                return None
    return None


def _job_definition(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": "youtube_sync",
            "label": "YouTube配信情報同期",
            "enabled": bool(config.get("youtube_sync_enabled")),
            "interval_minutes": int(config.get("youtube_sync_interval_minutes", 180)),
        },
        {
            "name": "reclassify",
            "label": "企画タグ・曜日再計算",
            "enabled": bool(config.get("reclassify_enabled")),
            "interval_minutes": int(config.get("reclassify_interval_minutes", 360)),
        },
        {
            "name": "backup",
            "label": "自動バックアップ",
            "enabled": bool(config.get("backup_enabled")),
            "interval_minutes": int(config.get("backup_interval_minutes", 1440)),
        },
    ]


def scheduler_status() -> dict[str, Any]:
    config = load_config()
    now = now_jst()
    jobs = []
    for definition in _job_definition(config):
        last = _last_success(definition["name"])
        interval = timedelta(minutes=definition["interval_minutes"])
        next_run = (last + interval) if last else now
        jobs.append({
            **definition,
            "last_success_at": last.isoformat(timespec="seconds") if last else None,
            "next_run_at": next_run.isoformat(timespec="seconds"),
            "due": bool(definition["enabled"] and next_run <= now),
        })
    return {
        "config": config,
        "jobs": jobs,
        "history": recent_history(15),
        "checked_at": now.isoformat(timespec="seconds"),
    }


def _run_job(job_name: str, reason: str) -> dict[str, Any]:
    started = now_jst()
    item: dict[str, Any] = {
        "job": job_name,
        "reason": reason,
        "status": "running",
        "started_at": started.isoformat(timespec="seconds"),
    }
    try:
        if job_name == "youtube_sync":
            status = connection_status()
            if not status.get("connected"):
                raise RuntimeError("YouTubeアカウントが未接続です。")
            result = sync_live_streams(max_pages=20)
        elif job_name == "reclassify":
            result = reclassify_all_streams(recompute_weekday=True)
        elif job_name == "backup":
            config = load_config()
            result = create_backup(
                include_chat_data=bool(config.get("backup_include_chat_data", False)),
                reason=f"background:{reason}",
            )
            result = {
                **result,
                "path": str(result.get("path", "")),
            }
        else:
            raise ValueError(f"未対応の定期処理です: {job_name}")

        item.update({
            "status": "success",
            "finished_at": now_jst().isoformat(timespec="seconds"),
            "duration_seconds": round((now_jst() - started).total_seconds(), 2),
            "result": result,
        })
    except Exception as exc:
        item.update({
            "status": "error",
            "finished_at": now_jst().isoformat(timespec="seconds"),
            "duration_seconds": round((now_jst() - started).total_seconds(), 2),
            "error": str(exc),
            "traceback": traceback.format_exc(limit=5),
        })
    _append_history(item)
    record_audit(
        f"background:{job_name}",
        status=item["status"],
        details={
            "reason": reason,
            "duration_seconds": item.get("duration_seconds"),
            "error": item.get("error", ""),
            "result": item.get("result", {}),
        },
    )
    return item


class BackgroundJobManager:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running_jobs: set[str] = set()
        self._run_lock = threading.RLock()

    def start(self) -> None:
        with self._run_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="vtuber-analytics-background-jobs",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)

    def wake(self) -> None:
        self._wake_event.set()

    def run_now(self, job_name: str) -> dict[str, Any]:
        with self._run_lock:
            if job_name in self._running_jobs:
                raise ValueError("この処理はすでに実行中です。")
            self._running_jobs.add(job_name)

        def worker() -> None:
            try:
                _run_job(job_name, "manual")
            finally:
                with self._run_lock:
                    self._running_jobs.discard(job_name)
                self.wake()

        threading.Thread(
            target=worker,
            name=f"vta-job-{job_name}",
            daemon=True,
        ).start()
        return {"accepted": True, "job": job_name}

    def state(self) -> dict[str, Any]:
        data = scheduler_status()
        with self._run_lock:
            data["running_jobs"] = sorted(self._running_jobs)
        data["thread_alive"] = bool(self._thread and self._thread.is_alive())
        return data

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            config = load_config()
            if bool(config.get("enabled", True)):
                now = now_jst()
                for definition in _job_definition(config):
                    if not definition["enabled"]:
                        continue
                    job_name = definition["name"]
                    with self._run_lock:
                        if job_name in self._running_jobs:
                            continue
                    last = _last_success(job_name)
                    due_at = (
                        last + timedelta(minutes=definition["interval_minutes"])
                        if last
                        else now
                    )
                    if due_at <= now:
                        with self._run_lock:
                            self._running_jobs.add(job_name)
                        try:
                            _run_job(job_name, "scheduled")
                        finally:
                            with self._run_lock:
                                self._running_jobs.discard(job_name)

            self._wake_event.wait(timeout=60)
            self._wake_event.clear()


BACKGROUND_JOB_MANAGER = BackgroundJobManager()
