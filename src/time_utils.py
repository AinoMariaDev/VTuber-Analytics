from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def now_jst() -> datetime:
    """日本時間の現在日時を、既存データと互換性のあるnaive datetimeで返す。"""
    return datetime.now(JST).replace(tzinfo=None)


def iso_now_jst() -> str:
    return now_jst().isoformat(timespec="seconds")
