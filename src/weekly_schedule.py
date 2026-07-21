from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from storage_paths import WEEKLY_SCHEDULE_DIR

SCHEDULE_DIR = WEEKLY_SCHEDULE_DIR


def _parse_date(value: str | None) -> date:
    if value:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("週の開始日は YYYY-MM-DD 形式で指定してください。") from exc
    return date.today()


def monday_of(value: str | None = None) -> date:
    target = _parse_date(value)
    return target - timedelta(days=target.weekday())


def _schedule_path(week_start: date) -> Path:
    return SCHEDULE_DIR / f"{week_start.isoformat()}.json"


def _empty_week(week_start: date) -> dict[str, Any]:
    days = []
    weekday_labels = ["月", "火", "水", "木", "金", "土", "日"]
    for offset, label in enumerate(weekday_labels):
        current = week_start + timedelta(days=offset)
        days.append({
            "date": current.isoformat(),
            "weekday": label,
            "type": "その他",
            "title": "",
            "note": "",
        })
    return {
        "week_start": week_start.isoformat(),
        "week_end": (week_start + timedelta(days=6)).isoformat(),
        "points": "",
        "requests": "",
        "days": days,
    }


def load_week(week_start_value: str | None = None) -> dict[str, Any]:
    week_start = monday_of(week_start_value)
    result = _empty_week(week_start)
    path = _schedule_path(week_start)
    if not path.exists():
        return result

    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return result

    result["points"] = str(saved.get("points", ""))
    result["requests"] = str(saved.get("requests", ""))

    saved_by_date = {
        str(item.get("date", "")): item
        for item in saved.get("days", [])
        if isinstance(item, dict)
    }
    for day in result["days"]:
        item = saved_by_date.get(day["date"], {})
        day["type"] = str(item.get("type", "その他")) or "その他"
        day["title"] = str(item.get("title", ""))
        day["note"] = str(item.get("note", ""))
    return result


def save_week(payload: dict[str, Any]) -> dict[str, Any]:
    week_start = monday_of(str(payload.get("week_start", "")) or None)
    base = _empty_week(week_start)
    base["points"] = str(payload.get("points", "")).strip()
    base["requests"] = str(payload.get("requests", "")).strip()
    incoming = payload.get("days", [])
    if not isinstance(incoming, list):
        raise ValueError("週間予定の形式が正しくありません。")

    incoming_by_date = {
        str(item.get("date", "")): item
        for item in incoming
        if isinstance(item, dict)
    }
    for day in base["days"]:
        item = incoming_by_date.get(day["date"], {})
        schedule_type = str(item.get("type", "その他")).strip() or "その他"
        allowed_types = {"休み", "雀魂", "ポケモン", "雑談", "その他"}
        day["type"] = schedule_type if schedule_type in allowed_types else "その他"
        day["title"] = str(item.get("title", "")).strip()
        day["note"] = str(item.get("note", "")).strip()

    SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)
    path = _schedule_path(week_start)
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(base, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)
    return {"ok": True, "schedule": base}
