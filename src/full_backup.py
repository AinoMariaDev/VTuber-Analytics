from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from storage_paths import BACKUP_DIR, DB_PATH, PROJECT_DIR


FILE_TARGETS = [
    "app_config.local.json",
    "moderation_rules.local.json",
]
DIRECTORY_TARGETS = [
    "data/weekly_schedules",
    "data/weekly_schedule_images",
    "youtube_chat_data",
    "reports",
    "exports",
]
ALLOWED_RESTORE_PREFIXES = tuple(FILE_TARGETS + DIRECTORY_TARGETS + ["data/vtuber_analytics.db"])


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _safe_arcname(path: Path) -> str:
    return path.relative_to(PROJECT_DIR).as_posix()


def _add_tree(zf: zipfile.ZipFile, directory: Path) -> int:
    count = 0
    if not directory.exists():
        return count
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            zf.write(path, _safe_arcname(path))
            count += 1
    return count


def _sqlite_snapshot(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(DB_PATH)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def create_backup(include_chat_data: bool = True, reason: str = "manual") -> dict[str, Any]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    output = BACKUP_DIR / f"VTuberAnalytics_backup_{stamp}.zip"

    with tempfile.TemporaryDirectory(prefix="vta_backup_") as temp_dir:
        snapshot = Path(temp_dir) / "data" / "vtuber_analytics.db"
        if DB_PATH.exists():
            _sqlite_snapshot(snapshot)

        entries: list[str] = []
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            if snapshot.exists():
                zf.write(snapshot, "data/vtuber_analytics.db")
                entries.append("data/vtuber_analytics.db")

            for relative in FILE_TARGETS:
                path = PROJECT_DIR / relative
                if path.exists() and path.is_file():
                    zf.write(path, relative)
                    entries.append(relative)

            directories = list(DIRECTORY_TARGETS)
            if not include_chat_data:
                directories.remove("youtube_chat_data")
            for relative in directories:
                directory = PROJECT_DIR / relative
                if directory.exists():
                    for path in sorted(directory.rglob("*")):
                        if path.is_file():
                            arcname = _safe_arcname(path)
                            zf.write(path, arcname)
                            entries.append(arcname)

            manifest = {
                "format": "vtuber-analytics-backup",
                "format_version": 1,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "reason": reason,
                "include_chat_data": include_chat_data,
                "entry_count": len(entries),
            }
            zf.writestr("backup_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    result = {
        "path": output,
        "filename": output.name,
        "size_bytes": output.stat().st_size,
        "created_at": datetime.fromtimestamp(output.stat().st_mtime).isoformat(timespec="seconds"),
    }

    from recovery_audit import (
        cleanup_old_backups,
        load_recovery_config,
        record_audit,
        verify_backup,
    )

    config = load_recovery_config()
    if bool(config.get("verify_after_create", True)):
        verification = verify_backup(output)
        result["verification"] = verification
        if not verification.get("ok"):
            record_audit(
                "backup_create",
                status="error",
                details={
                    "filename": output.name,
                    "verification": verification,
                },
            )
            raise RuntimeError(
                "バックアップは作成されましたが、整合性確認に失敗しました。"
            )

    result["cleanup"] = cleanup_old_backups()
    record_audit(
        "backup_create",
        status="success",
        details={
            "filename": output.name,
            "size_bytes": result["size_bytes"],
            "reason": reason,
            "include_chat_data": include_chat_data,
        },
    )
    return result


def list_backups(limit: int = 20) -> list[dict[str, Any]]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for path in sorted(BACKUP_DIR.glob("VTuberAnalytics_backup_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        items.append({
            "filename": path.name,
            "size_bytes": path.stat().st_size,
            "created_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        })
    return items


def _validate_member(name: str) -> str:
    normalized = name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"危険なパスを含むファイルがあります: {name}")
    if normalized == "backup_manifest.json":
        return normalized
    if not any(normalized == prefix or normalized.startswith(prefix.rstrip("/") + "/") for prefix in ALLOWED_RESTORE_PREFIXES):
        raise ValueError(f"復元対象外のファイルが含まれています: {name}")
    return normalized


def restore_backup(upload_stream: BinaryIO, content_length: int) -> dict[str, Any]:
    from recovery_audit import record_audit, verify_backup

    if content_length <= 0:
        raise ValueError("復元するZIPファイルが空です。")
    if content_length > 5 * 1024 * 1024 * 1024:
        raise ValueError("復元ZIPが大きすぎます。")

    safety = create_backup(include_chat_data=True, reason="before_restore")

    with tempfile.TemporaryDirectory(prefix="vta_restore_") as temp_dir:
        temp_root = Path(temp_dir)
        upload_path = temp_root / "restore.zip"
        remaining = content_length
        with upload_path.open("wb") as out:
            while remaining > 0:
                chunk = upload_stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
        if remaining != 0:
            raise ValueError("ZIPファイルを最後まで読み込めませんでした。")

        verification = verify_backup(upload_path)
        if not verification.get("ok"):
            record_audit(
                "backup_restore",
                status="error",
                details={"verification": verification},
            )
            errors = "／".join(verification.get("errors", []))
            raise ValueError(
                "復元前のバックアップ検証に失敗しました。"
                + (f" {errors}" if errors else "")
            )

        try:
            zf = zipfile.ZipFile(upload_path, "r")
        except zipfile.BadZipFile as exc:
            raise ValueError("有効なZIPファイルではありません。") from exc

        with zf:
            names = [_validate_member(info.filename) for info in zf.infolist() if not info.is_dir()]
            if "backup_manifest.json" not in names:
                raise ValueError("VTuber Analyticsのバックアップではありません。")
            manifest = json.loads(zf.read("backup_manifest.json").decode("utf-8"))
            if manifest.get("format") != "vtuber-analytics-backup":
                raise ValueError("対応していないバックアップ形式です。")

            extract_root = temp_root / "extracted"
            for info in zf.infolist():
                if info.is_dir():
                    continue
                safe_name = _validate_member(info.filename)
                if safe_name == "backup_manifest.json":
                    continue
                destination = extract_root / PurePosixPath(safe_name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)

            restored = []
            for relative in FILE_TARGETS:
                source = extract_root / relative
                if source.exists():
                    target = PROJECT_DIR / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, target)
                    restored.append(relative)

            db_source = extract_root / "data" / "vtuber_analytics.db"
            if db_source.exists():
                DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(str(DB_PATH) + suffix)
                    if sidecar.exists():
                        sidecar.unlink()
                os.replace(db_source, DB_PATH)
                restored.append("data/vtuber_analytics.db")

            for relative in DIRECTORY_TARGETS:
                source_dir = extract_root / relative
                if not source_dir.exists():
                    continue
                target_dir = PROJECT_DIR / relative
                target_dir.mkdir(parents=True, exist_ok=True)
                for source in source_dir.rglob("*"):
                    if source.is_file():
                        target = target_dir / source.relative_to(source_dir)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, target)
                        restored.append(_safe_arcname(target))

    result = {
        "ok": True,
        "restored_count": len(restored),
        "safety_backup": safety["filename"],
        "message": "復元が完了しました。アプリを再起動してください。",
    }
    record_audit(
        "backup_restore",
        status="success",
        details={
            "restored_count": len(restored),
            "safety_backup": safety["filename"],
        },
    )
    return result
