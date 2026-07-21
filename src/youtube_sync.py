from __future__ import annotations

import json
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from time_utils import now_jst

from storage_paths import (
    DB_PATH,
    YOUTUBE_OAUTH_CONFIG_PATH,
    YOUTUBE_TOKEN_PATH,
)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/youtube/v3"
SCOPE = "https://www.googleapis.com/auth/youtube.readonly"

_PENDING_STATES: dict[str, int] = {}
STATE_TTL_SECONDS = 10 * 60


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{path.name}を読み込めません: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{path.name}の形式が正しくありません。")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def public_settings() -> dict[str, Any]:
    config = _read_json(YOUTUBE_OAUTH_CONFIG_PATH, {})
    return {
        "client_id": str(config.get("client_id", "")),
        "client_secret_set": bool(config.get("client_secret")),
        "redirect_uri": str(
            config.get(
                "redirect_uri",
                "http://localhost:8765/api/youtube/oauth/callback",
            )
        ),
    }


def save_settings(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict[str, Any]:
    current = _read_json(YOUTUBE_OAUTH_CONFIG_PATH, {})
    normalized_id = client_id.strip()
    normalized_redirect = redirect_uri.strip()
    if not normalized_id:
        raise ValueError("Google OAuthクライアントIDを入力してください。")
    if not normalized_redirect.startswith(("http://", "https://")):
        raise ValueError("リダイレクトURIはhttpまたはhttpsで始めてください。")

    secret = client_secret.strip() or str(current.get("client_secret", ""))
    if not secret:
        raise ValueError("Google OAuthクライアントシークレットを入力してください。")

    data = {
        "client_id": normalized_id,
        "client_secret": secret,
        "redirect_uri": normalized_redirect,
        "updated_at": now_jst().isoformat(timespec="seconds"),
    }
    _write_json(YOUTUBE_OAUTH_CONFIG_PATH, data)
    return public_settings()


def _oauth_config() -> dict[str, str]:
    data = _read_json(YOUTUBE_OAUTH_CONFIG_PATH, {})
    required = ("client_id", "client_secret", "redirect_uri")
    missing = [name for name in required if not str(data.get(name, "")).strip()]
    if missing:
        raise ValueError("YouTube OAuth設定が未完了です。")
    return {name: str(data[name]).strip() for name in required}


def create_authorization_url() -> str:
    config = _oauth_config()
    now = int(time.time())
    expired = [
        state
        for state, created_at in _PENDING_STATES.items()
        if created_at + STATE_TTL_SECONDS < now
    ]
    for state in expired:
        _PENDING_STATES.pop(state, None)

    state = secrets.token_urlsafe(32)
    _PENDING_STATES[state] = now
    params = {
        "client_id": config["client_id"],
        "redirect_uri": config["redirect_uri"],
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def _post_form(url: str, values: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(values).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Google認証APIエラー ({exc.code}): {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Google認証APIへ接続できません: {exc.reason}") from exc


def exchange_code(code: str, state: str) -> dict[str, Any]:
    created_at = _PENDING_STATES.pop(state, None)
    if created_at is None or created_at + STATE_TTL_SECONDS < int(time.time()):
        raise ValueError("Google認証の状態確認に失敗しました。最初から接続し直してください。")
    if not code:
        raise ValueError("Googleから認証コードが返されませんでした。")

    config = _oauth_config()
    token = _post_form(
        TOKEN_URL,
        {
            "code": code,
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "redirect_uri": config["redirect_uri"],
            "grant_type": "authorization_code",
        },
    )
    now = int(time.time())
    existing = _read_json(YOUTUBE_TOKEN_PATH, {})
    if not token.get("refresh_token") and existing.get("refresh_token"):
        token["refresh_token"] = existing["refresh_token"]
    token["obtained_at"] = now
    token["expires_at"] = now + int(token.get("expires_in", 3600))
    _write_json(YOUTUBE_TOKEN_PATH, token)
    channel = get_my_channel()
    token_store = _read_json(YOUTUBE_TOKEN_PATH, {})
    token_store["channel"] = channel
    token_store["connected_at"] = now_jst().isoformat(timespec="seconds")
    _write_json(YOUTUBE_TOKEN_PATH, token_store)
    return channel


def _refresh_access_token(token: dict[str, Any]) -> dict[str, Any]:
    refresh_token = str(token.get("refresh_token", ""))
    if not refresh_token:
        raise RuntimeError("再認証が必要です。Google連携をやり直してください。")
    config = _oauth_config()
    refreshed = _post_form(
        TOKEN_URL,
        {
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    token.update(refreshed)
    token["refresh_token"] = refresh_token
    now = int(time.time())
    token["obtained_at"] = now
    token["expires_at"] = now + int(refreshed.get("expires_in", 3600))
    _write_json(YOUTUBE_TOKEN_PATH, token)
    return token


def _access_token() -> str:
    token = _read_json(YOUTUBE_TOKEN_PATH, {})
    if not token.get("access_token"):
        raise ValueError("YouTubeアカウントが未接続です。")
    if int(token.get("expires_at", 0)) <= int(time.time()) + 60:
        token = _refresh_access_token(token)
    return str(token["access_token"])


def _api_get(resource: str, params: dict[str, Any]) -> dict[str, Any]:
    url = f"{API_BASE}/{resource}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {_access_token()}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"YouTube APIエラー ({exc.code}): {detail[:700]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"YouTube APIへ接続できません: {exc.reason}") from exc


def get_my_channel() -> dict[str, Any]:
    data = _api_get(
        "channels",
        {
            "part": "snippet,contentDetails",
            "mine": "true",
            "maxResults": 1,
        },
    )
    items = data.get("items", [])
    if not items:
        raise RuntimeError("接続したGoogleアカウントにYouTubeチャンネルがありません。")
    item = items[0]
    return {
        "channel_id": item["id"],
        "title": item.get("snippet", {}).get("title", ""),
        "uploads_playlist_id": (
            item.get("contentDetails", {})
            .get("relatedPlaylists", {})
            .get("uploads", "")
        ),
    }


def connection_status() -> dict[str, Any]:
    settings = public_settings()
    token = _read_json(YOUTUBE_TOKEN_PATH, {})
    return {
        "configured": bool(settings["client_id"] and settings["client_secret_set"]),
        "connected": bool(token.get("refresh_token") or token.get("access_token")),
        "channel": token.get("channel"),
        "connected_at": token.get("connected_at"),
        "last_sync": token.get("last_sync"),
        "settings": settings,
    }


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_minutes(start: str | None, end: str | None) -> int | None:
    started = _parse_datetime(start)
    ended = _parse_datetime(end)
    if not started or not ended:
        return None
    return max(0, round((ended - started).total_seconds() / 60))


def ensure_sync_schema(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(streams)").fetchall()
    }
    additions = {
        "description": "TEXT",
        "thumbnail_url": "TEXT",
        "published_at": "TEXT",
        "actual_start_time": "TEXT",
        "actual_end_time": "TEXT",
        "youtube_sync_status": "TEXT",
        "source_type": "TEXT",
        "last_synced_at": "TEXT",
    }
    for name, sql_type in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE streams ADD COLUMN {name} {sql_type}")


def sync_live_streams(max_pages: int = 20) -> dict[str, Any]:
    channel = get_my_channel()
    playlist_id = str(channel.get("uploads_playlist_id", ""))
    if not playlist_id:
        raise RuntimeError("アップロード再生リストを取得できませんでした。")

    video_ids: list[str] = []
    page_token = ""
    pages = 0
    while pages < max(1, min(max_pages, 100)):
        params: dict[str, Any] = {
            "part": "contentDetails",
            "playlistId": playlist_id,
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token
        data = _api_get("playlistItems", params)
        for item in data.get("items", []):
            video_id = str(item.get("contentDetails", {}).get("videoId", ""))
            if video_id:
                video_ids.append(video_id)
        pages += 1
        page_token = str(data.get("nextPageToken", ""))
        if not page_token:
            break

    live_items: list[dict[str, Any]] = []
    for offset in range(0, len(video_ids), 50):
        batch = video_ids[offset:offset + 50]
        data = _api_get(
            "videos",
            {
                "part": "snippet,liveStreamingDetails,status",
                "id": ",".join(batch),
                "maxResults": 50,
            },
        )
        for item in data.get("items", []):
            details = item.get("liveStreamingDetails")
            if not isinstance(details, dict):
                continue
            live_items.append(item)

    inserted = 0
    updated = 0
    now_text = now_jst().isoformat(timespec="seconds")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as connection:
        ensure_sync_schema(connection)
        existing_ids = {
            str(row[0])
            for row in connection.execute("SELECT video_id FROM streams").fetchall()
        }

        for item in live_items:
            video_id = str(item["id"])
            snippet = item.get("snippet", {})
            details = item.get("liveStreamingDetails", {})
            published_at = str(snippet.get("publishedAt", ""))
            actual_start = str(details.get("actualStartTime", ""))
            actual_end = str(details.get("actualEndTime", ""))
            date_source = actual_start or published_at
            stream_date = date_source[:10] if len(date_source) >= 10 else ""
            thumbnail = (
                snippet.get("thumbnails", {}).get("high", {}).get("url")
                or snippet.get("thumbnails", {}).get("medium", {}).get("url")
                or ""
            )
            duration = _duration_minutes(actual_start, actual_end)
            sync_status = (
                "completed" if actual_end
                else "live" if actual_start
                else "upcoming"
            )

            connection.execute(
                """
                INSERT INTO streams(
                    video_id, stream_date, title, source_file, imported_at,
                    duration_minutes, description, thumbnail_url, published_at,
                    actual_start_time, actual_end_time, youtube_sync_status,
                    source_type, last_synced_at
                )
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, 'youtube_api', ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    stream_date = CASE
                        WHEN excluded.stream_date <> '' THEN excluded.stream_date
                        ELSE streams.stream_date
                    END,
                    title = excluded.title,
                    duration_minutes = COALESCE(excluded.duration_minutes, streams.duration_minutes),
                    description = excluded.description,
                    thumbnail_url = excluded.thumbnail_url,
                    published_at = excluded.published_at,
                    actual_start_time = excluded.actual_start_time,
                    actual_end_time = excluded.actual_end_time,
                    youtube_sync_status = excluded.youtube_sync_status,
                    source_type = 'youtube_api',
                    last_synced_at = excluded.last_synced_at
                """,
                (
                    video_id,
                    stream_date,
                    str(snippet.get("title", "タイトル未設定")),
                    f"youtube_api:{video_id}",
                    duration,
                    str(snippet.get("description", "")),
                    str(thumbnail),
                    published_at,
                    actual_start,
                    actual_end,
                    sync_status,
                    now_text,
                ),
            )
            if video_id in existing_ids:
                updated += 1
            else:
                inserted += 1
                existing_ids.add(video_id)

        connection.commit()

    token = _read_json(YOUTUBE_TOKEN_PATH, {})
    result = {
        "synced_at": now_text,
        "channel": channel,
        "pages_checked": pages,
        "videos_checked": len(video_ids),
        "live_streams_found": len(live_items),
        "inserted": inserted,
        "updated": updated,
    }
    token["channel"] = channel
    token["last_sync"] = result
    _write_json(YOUTUBE_TOKEN_PATH, token)
    return result
