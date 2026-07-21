from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

from storage_paths import AUTH_USERS_PATH

PBKDF2_ITERATIONS = 310_000
SESSION_TTL_SECONDS = 8 * 60 * 60
SESSION_COOKIE_NAME = "vta_session"

ROLE_LABELS = {
    "admin": "管理者",
    "moderator": "モデレーター",
    "viewer": "閲覧のみ",
}
ROLE_ORDER = {"viewer": 1, "moderator": 2, "admin": 3}

_LOCK = threading.RLock()
_SESSIONS: dict[str, dict[str, Any]] = {}


@dataclass(frozen=True)
class AuthUser:
    username: str
    display_name: str
    role: str

    def public(self) -> dict[str, str]:
        return {
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "role_label": ROLE_LABELS.get(self.role, self.role),
        }


def _normalize_username(value: str) -> str:
    username = value.strip().lower()
    if not username:
        raise ValueError("ユーザー名を入力してください。")
    if len(username) < 3 or len(username) > 40:
        raise ValueError("ユーザー名は3〜40文字で入力してください。")
    if not all(ch.isalnum() or ch in "._-" for ch in username):
        raise ValueError("ユーザー名は英数字・ピリオド・ハイフン・アンダーバーのみ使用できます。")
    return username


def _validate_password(password: str) -> None:
    if len(password) < 10:
        raise ValueError("パスワードは10文字以上にしてください。")
    if len(password) > 200:
        raise ValueError("パスワードが長すぎます。")


def _hash_password(password: str, salt: bytes | None = None) -> dict[str, str | int]:
    _validate_password(password)
    actual_salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        actual_salt,
        PBKDF2_ITERATIONS,
    )
    return {
        "algorithm": "pbkdf2_sha256",
        "iterations": PBKDF2_ITERATIONS,
        "salt": actual_salt.hex(),
        "hash": digest.hex(),
    }


def _verify_password(password: str, record: dict[str, Any]) -> bool:
    try:
        salt = bytes.fromhex(str(record["salt"]))
        expected = bytes.fromhex(str(record["hash"]))
        iterations = int(record.get("iterations", PBKDF2_ITERATIONS))
    except (KeyError, TypeError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual, expected)


def _load_store() -> dict[str, Any]:
    if not AUTH_USERS_PATH.exists():
        return {"version": 1, "users": []}
    try:
        data = json.loads(AUTH_USERS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"認証ユーザーファイルを読み込めません: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("users"), list):
        raise RuntimeError("認証ユーザーファイルの形式が正しくありません。")
    return data


def _save_store(data: dict[str, Any]) -> None:
    AUTH_USERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = AUTH_USERS_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(AUTH_USERS_PATH)


def setup_required() -> bool:
    with _LOCK:
        return not _load_store().get("users")


def create_initial_admin(
    username: str,
    display_name: str,
    password: str,
) -> dict[str, str]:
    with _LOCK:
        store = _load_store()
        if store.get("users"):
            raise ValueError("初期管理者はすでに作成されています。")
        normalized = _normalize_username(username)
        password_data = _hash_password(password)
        user = {
            "username": normalized,
            "display_name": display_name.strip() or normalized,
            "role": "admin",
            **password_data,
        }
        store["users"] = [user]
        _save_store(store)
        return AuthUser(normalized, user["display_name"], "admin").public()


def authenticate(username: str, password: str) -> AuthUser | None:
    normalized = username.strip().lower()
    with _LOCK:
        store = _load_store()
        for item in store.get("users", []):
            if str(item.get("username", "")).lower() != normalized:
                continue
            if not _verify_password(password, item):
                return None
            role = str(item.get("role", "viewer"))
            if role not in ROLE_ORDER:
                role = "viewer"
            return AuthUser(
                username=str(item["username"]),
                display_name=str(item.get("display_name") or item["username"]),
                role=role,
            )
    return None


def create_session(user: AuthUser) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with _LOCK:
        _cleanup_sessions(now)
        _SESSIONS[token] = {
            "user": user.public(),
            "created_at": now,
            "expires_at": now + SESSION_TTL_SECONDS,
        }
    return token


def _cleanup_sessions(now: int | None = None) -> None:
    current = now or int(time.time())
    expired = [
        token
        for token, value in _SESSIONS.items()
        if int(value.get("expires_at", 0)) <= current
    ]
    for token in expired:
        _SESSIONS.pop(token, None)


def parse_session_token(cookie_header: str | None) -> str:
    if not cookie_header:
        return ""
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return ""
    morsel = cookie.get(SESSION_COOKIE_NAME)
    return morsel.value if morsel else ""


def current_user(cookie_header: str | None) -> AuthUser | None:
    token = parse_session_token(cookie_header)
    if not token:
        return None
    now = int(time.time())
    with _LOCK:
        _cleanup_sessions(now)
        session = _SESSIONS.get(token)
        if not session:
            return None
        session["expires_at"] = now + SESSION_TTL_SECONDS
        data = session["user"]
        return AuthUser(
            username=str(data["username"]),
            display_name=str(data["display_name"]),
            role=str(data["role"]),
        )


def destroy_session(cookie_header: str | None) -> None:
    token = parse_session_token(cookie_header)
    if not token:
        return
    with _LOCK:
        _SESSIONS.pop(token, None)


def role_at_least(user: AuthUser | None, required_role: str) -> bool:
    if user is None:
        return False
    return ROLE_ORDER.get(user.role, 0) >= ROLE_ORDER.get(required_role, 99)


def session_cookie(token: str, secure: bool = False) -> str:
    parts = [
        f"{SESSION_COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={SESSION_TTL_SECONDS}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_session_cookie() -> str:
    return (
        f"{SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; "
        "Max-Age=0"
    )
