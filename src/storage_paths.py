from __future__ import annotations

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent

def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else default

DATA_DIR = _env_path("VTA_DATA_DIR", PROJECT_DIR / "data")
DB_PATH = _env_path("VTA_DB_PATH", DATA_DIR / "vtuber_analytics.db")
CONFIG_PATH = _env_path("VTA_CONFIG_PATH", DATA_DIR / "app_config.json" if os.environ.get("VTA_SERVER_MODE") == "1" else PROJECT_DIR / "app_config.local.json")
MODERATION_RULES_PATH = _env_path("VTA_MODERATION_RULES_PATH", DATA_DIR / "moderation_rules.json" if os.environ.get("VTA_SERVER_MODE") == "1" else PROJECT_DIR / "moderation_rules.local.json")
AUTH_USERS_PATH = _env_path("VTA_AUTH_USERS_PATH", DATA_DIR / "auth_users.json")
YOUTUBE_OAUTH_CONFIG_PATH = _env_path("VTA_YOUTUBE_OAUTH_CONFIG_PATH", DATA_DIR / "youtube_oauth.json")
YOUTUBE_TOKEN_PATH = _env_path("VTA_YOUTUBE_TOKEN_PATH", DATA_DIR / "youtube_token.json")
BACKGROUND_JOBS_CONFIG_PATH = _env_path("VTA_BACKGROUND_JOBS_CONFIG_PATH", DATA_DIR / "background_jobs.json")
BACKGROUND_JOBS_HISTORY_PATH = _env_path("VTA_BACKGROUND_JOBS_HISTORY_PATH", DATA_DIR / "background_jobs_history.json")
RECOVERY_CONFIG_PATH = _env_path("VTA_RECOVERY_CONFIG_PATH", DATA_DIR / "recovery_config.json")
AUDIT_LOG_PATH = _env_path("VTA_AUDIT_LOG_PATH", DATA_DIR / "audit_log.json")
CHAT_DIR = _env_path("VTA_CHAT_DIR", DATA_DIR / "youtube_chat_data" if os.environ.get("VTA_SERVER_MODE") == "1" else PROJECT_DIR / "youtube_chat_data")
BACKUP_DIR = _env_path("VTA_BACKUP_DIR", DATA_DIR / "backups" if os.environ.get("VTA_SERVER_MODE") == "1" else PROJECT_DIR / "backups")
REPORT_DIR = _env_path("VTA_REPORT_DIR", DATA_DIR / "reports" if os.environ.get("VTA_SERVER_MODE") == "1" else PROJECT_DIR / "reports")
EXPORT_DIR = _env_path("VTA_EXPORT_DIR", DATA_DIR / "exports" if os.environ.get("VTA_SERVER_MODE") == "1" else PROJECT_DIR / "exports")
WEEKLY_SCHEDULE_DIR = _env_path("VTA_WEEKLY_SCHEDULE_DIR", DATA_DIR / "weekly_schedules")
WEEKLY_IMAGE_DIR = _env_path("VTA_WEEKLY_IMAGE_DIR", DATA_DIR / "weekly_schedule_images")
LOG_DIR = _env_path("VTA_LOG_DIR", DATA_DIR / "logs")

def ensure_storage_directories() -> None:
    for path in (DATA_DIR, DB_PATH.parent, CONFIG_PATH.parent, MODERATION_RULES_PATH.parent, AUTH_USERS_PATH.parent, YOUTUBE_OAUTH_CONFIG_PATH.parent, YOUTUBE_TOKEN_PATH.parent, BACKGROUND_JOBS_CONFIG_PATH.parent, BACKGROUND_JOBS_HISTORY_PATH.parent, RECOVERY_CONFIG_PATH.parent, AUDIT_LOG_PATH.parent, CHAT_DIR, BACKUP_DIR, REPORT_DIR, EXPORT_DIR, WEEKLY_SCHEDULE_DIR, WEEKLY_IMAGE_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)

def storage_summary() -> dict[str, str]:
    return {
        "project_dir": str(PROJECT_DIR), "data_dir": str(DATA_DIR), "database": str(DB_PATH),
        "config": str(CONFIG_PATH), "moderation_rules": str(MODERATION_RULES_PATH), "auth_users": str(AUTH_USERS_PATH), "youtube_oauth": str(YOUTUBE_OAUTH_CONFIG_PATH), "youtube_token": str(YOUTUBE_TOKEN_PATH), "background_jobs": str(BACKGROUND_JOBS_CONFIG_PATH), "background_jobs_history": str(BACKGROUND_JOBS_HISTORY_PATH), "recovery_config": str(RECOVERY_CONFIG_PATH), "audit_log": str(AUDIT_LOG_PATH),
        "chat_dir": str(CHAT_DIR), "backup_dir": str(BACKUP_DIR), "report_dir": str(REPORT_DIR),
        "export_dir": str(EXPORT_DIR), "weekly_schedule_dir": str(WEEKLY_SCHEDULE_DIR),
        "weekly_image_dir": str(WEEKLY_IMAGE_DIR), "log_dir": str(LOG_DIR),
    }
