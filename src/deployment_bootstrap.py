from __future__ import annotations
import json, os, shutil, sqlite3
from pathlib import Path
from typing import Any
from storage_paths import (
    AUTH_USERS_PATH, BACKGROUND_JOBS_CONFIG_PATH, BACKGROUND_JOBS_HISTORY_PATH,
    CONFIG_PATH, DB_PATH, MODERATION_RULES_PATH, PROJECT_DIR,
    RECOVERY_CONFIG_PATH, YOUTUBE_OAUTH_CONFIG_PATH, YOUTUBE_TOKEN_PATH,
    ensure_storage_directories,
)

def _copy_if_missing(source: Path, destination: Path) -> bool:
    if destination.exists() or not source.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return True

def _write_json_if_missing(path: Path, value: Any) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return True

def _validate_database(path: Path) -> None:
    if not path.exists():
        raise RuntimeError(f"Database not found after bootstrap: {path}")
    connection=sqlite3.connect(path)
    try:
        result=connection.execute("PRAGMA quick_check").fetchone()
        if not result or result[0]!="ok":
            raise RuntimeError(f"Database integrity check failed: {result}")
    finally:
        connection.close()

def bootstrap_persistent_storage() -> dict[str, Any]:
    ensure_storage_directories()
    copied=[]
    created=[]
    for source,destination in (
        (PROJECT_DIR/"data"/"vtuber_analytics.db", DB_PATH),
        (PROJECT_DIR/"app_config.local.json", CONFIG_PATH),
        (PROJECT_DIR/"moderation_rules.local.json", MODERATION_RULES_PATH),
    ):
        if _copy_if_missing(source,destination):
            copied.append(str(destination))
    defaults={
        BACKGROUND_JOBS_CONFIG_PATH:{
            "enabled":True,"youtube_sync_enabled":True,
            "youtube_sync_interval_minutes":180,
            "reclassify_enabled":True,"reclassify_interval_minutes":360,
            "backup_enabled":True,"backup_interval_minutes":1440,
            "backup_include_chat_data":False,"history_limit":100,
        },
        BACKGROUND_JOBS_HISTORY_PATH:[],
        RECOVERY_CONFIG_PATH:{"max_backup_generations":20,"verify_after_create":True},
    }
    for path,value in defaults.items():
        if _write_json_if_missing(path,value):
            created.append(str(path))
    for path in (AUTH_USERS_PATH,YOUTUBE_OAUTH_CONFIG_PATH,YOUTUBE_TOKEN_PATH):
        path.parent.mkdir(parents=True,exist_ok=True)
    _validate_database(DB_PATH)
    return {"database":str(DB_PATH),"config":str(CONFIG_PATH),
            "copied":copied,"created":created,"render":bool(os.environ.get("RENDER"))}
