from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from time_utils import now_jst

from storage_paths import (
    AUDIT_LOG_PATH,
    BACKUP_DIR,
    DB_PATH,
    RECOVERY_CONFIG_PATH,
)

DEFAULT_RECOVERY_CONFIG = {
    "max_backup_generations": 20,
    "verify_after_create": True,
}


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
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(path)


def load_recovery_config() -> dict[str, Any]:
    data = _read_json(RECOVERY_CONFIG_PATH, {})
    config = dict(DEFAULT_RECOVERY_CONFIG)
    if isinstance(data, dict):
        config.update(data)
    return config


def save_recovery_config(payload: dict[str, Any]) -> dict[str, Any]:
    config = load_recovery_config()
    if "max_backup_generations" in payload:
        generations = int(payload["max_backup_generations"])
        if generations < 3 or generations > 200:
            raise ValueError("バックアップ世代数は3〜200で指定してください。")
        config["max_backup_generations"] = generations
    if "verify_after_create" in payload:
        config["verify_after_create"] = bool(payload["verify_after_create"])
    _write_json(RECOVERY_CONFIG_PATH, config)
    return config


def record_audit(
    action: str,
    *,
    status: str,
    actor: str = "system",
    role: str = "system",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = {
        "occurred_at": now_jst().isoformat(timespec="seconds"),
        "action": action,
        "status": status,
        "actor": actor,
        "role": role,
        "details": details or {},
    }
    history = _read_json(AUDIT_LOG_PATH, [])
    if not isinstance(history, list):
        history = []
    history.insert(0, item)
    history = history[:1000]
    _write_json(AUDIT_LOG_PATH, history)
    return item


def recent_audit(limit: int = 100) -> list[dict[str, Any]]:
    history = _read_json(AUDIT_LOG_PATH, [])
    if not isinstance(history, list):
        return []
    return history[: max(1, min(limit, 500))]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_backup(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "filename": path.name,
        "path": str(path),
        "exists": path.exists(),
        "valid_zip": False,
        "manifest_valid": False,
        "database_valid": False,
        "entry_count": 0,
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "sha256": "",
        "errors": [],
        "verified_at": now_jst().isoformat(timespec="seconds"),
    }
    if not path.exists():
        result["errors"].append("バックアップファイルがありません。")
        return result

    result["sha256"] = _sha256(path)

    try:
        with zipfile.ZipFile(path, "r") as archive:
            damaged = archive.testzip()
            if damaged:
                result["errors"].append(f"破損したZIPエントリがあります: {damaged}")
                return result
            result["valid_zip"] = True
            names = [info.filename for info in archive.infolist() if not info.is_dir()]
            result["entry_count"] = len(names)

            if "backup_manifest.json" not in names:
                result["errors"].append("バックアップ識別情報がありません。")
                return result

            try:
                manifest = json.loads(
                    archive.read("backup_manifest.json").decode("utf-8")
                )
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                result["errors"].append(f"バックアップ識別情報を読めません: {exc}")
                return result

            if manifest.get("format") != "vtuber-analytics-backup":
                result["errors"].append("VTuber Analyticsのバックアップではありません。")
                return result
            result["manifest_valid"] = True
            result["manifest"] = manifest

            if "data/vtuber_analytics.db" not in names:
                result["errors"].append("データベースが含まれていません。")
                return result

            with tempfile.TemporaryDirectory(prefix="vta_verify_") as temp_dir:
                db_path = Path(temp_dir) / "verify.db"
                db_path.write_bytes(archive.read("data/vtuber_analytics.db"))
                try:
                    connection = sqlite3.connect(
                        f"file:{db_path}?mode=ro",
                        uri=True,
                        timeout=10,
                    )
                    try:
                        integrity = connection.execute(
                            "PRAGMA quick_check"
                        ).fetchone()
                        integrity_text = str(
                            integrity[0] if integrity else "unknown"
                        )
                        result["database_integrity"] = integrity_text
                        if integrity_text != "ok":
                            result["errors"].append(
                                f"データベース整合性エラー: {integrity_text}"
                            )
                            return result
                        tables = {
                            row[0]
                            for row in connection.execute(
                                "SELECT name FROM sqlite_master WHERE type='table'"
                            ).fetchall()
                        }
                        required = {"streams", "messages"}
                        missing = sorted(required - tables)
                        if missing:
                            result["errors"].append(
                                "必要なテーブルがありません: " + ", ".join(missing)
                            )
                            return result
                        result["stream_count"] = int(
                            connection.execute(
                                "SELECT COUNT(*) FROM streams"
                            ).fetchone()[0]
                        )
                        result["message_count"] = int(
                            connection.execute(
                                "SELECT COUNT(*) FROM messages"
                            ).fetchone()[0]
                        )
                    finally:
                        connection.close()
                except sqlite3.Error as exc:
                    result["errors"].append(f"データベースを開けません: {exc}")
                    return result

            result["database_valid"] = True
    except zipfile.BadZipFile:
        result["errors"].append("有効なZIPファイルではありません。")
    except OSError as exc:
        result["errors"].append(f"バックアップを確認できません: {exc}")

    result["ok"] = bool(
        result["valid_zip"]
        and result["manifest_valid"]
        and result["database_valid"]
        and not result["errors"]
    )
    return result


def verify_backup_by_name(filename: str) -> dict[str, Any]:
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name.endswith(".zip"):
        raise ValueError("バックアップファイル名が正しくありません。")
    return verify_backup(BACKUP_DIR / safe_name)


def verify_all_backups(limit: int = 50) -> list[dict[str, Any]]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    paths = sorted(
        BACKUP_DIR.glob("VTuberAnalytics_backup_*.zip"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )[: max(1, min(limit, 200))]
    return [verify_backup(path) for path in paths]


def cleanup_old_backups() -> dict[str, Any]:
    config = load_recovery_config()
    keep = int(config.get("max_backup_generations", 20))
    paths = sorted(
        BACKUP_DIR.glob("VTuberAnalytics_backup_*.zip"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    deleted = []
    for path in paths[keep:]:
        try:
            path.unlink()
            deleted.append(path.name)
        except OSError:
            continue
    return {
        "kept": min(len(paths), keep),
        "deleted_count": len(deleted),
        "deleted": deleted,
    }


def recovery_status() -> dict[str, Any]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backups = sorted(
        BACKUP_DIR.glob("VTuberAnalytics_backup_*.zip"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    latest_verification = verify_backup(backups[0]) if backups else None
    return {
        "config": load_recovery_config(),
        "backup_count": len(backups),
        "latest_backup": latest_verification,
        "audit": recent_audit(50),
        "database_exists": DB_PATH.exists(),
        "database_size_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
        "checked_at": now_jst().isoformat(timespec="seconds"),
    }
