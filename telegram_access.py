"""Durable Telegram access control and bounded callback-detail storage.

Access state is intentionally compact: raw Telegram updates and message history
are never persisted.  Both files belong under DATA_DIR in production.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
from copy import deepcopy
from typing import Any


DEFAULT_SETTINGS = {
    "paused": False,
    "send_mode": "useful",
    "cohort_mode": "important",
}
SETTING_VALUES = {
    "paused": {False, True},
    "send_mode": {"all", "useful"},
    "cohort_mode": {"all", "important", "off"},
}


class TelegramAccessStore:
    """Small atomically-written registry for bot access and admin settings."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._lock = threading.RLock()
        self._state = self._load()

    def _empty(self) -> dict[str, Any]:
        return {
            "version": 1,
            "update_offset": 0,
            "settings": dict(DEFAULT_SETTINGS),
            "authorized": {},
            "pending": {},
            "revoked": [],
        }

    def _load(self) -> dict[str, Any]:
        state = self._empty()
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            return state
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError(f"Повреждён Telegram access state {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"Telegram access state должен быть JSON-объектом: {self.path}")
        state["update_offset"] = max(0, int(raw.get("update_offset") or 0))
        settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
        for key, default in DEFAULT_SETTINGS.items():
            value = settings.get(key, default)
            state["settings"][key] = value if value in SETTING_VALUES[key] else default
        for key in ("authorized", "pending"):
            value = raw.get(key)
            if isinstance(value, dict):
                state[key] = {str(k): v for k, v in value.items() if isinstance(v, dict)}
        revoked = raw.get("revoked") or []
        state["revoked"] = sorted({str(value) for value in revoked if str(value)})
        return state

    def _save(self) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".telegram-access-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        except Exception:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            raise

    @property
    def update_offset(self) -> int:
        with self._lock:
            return int(self._state["update_offset"])

    def settings(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state["settings"])

    def recipients(self) -> list[str]:
        with self._lock:
            return list(self._state["authorized"])

    def revoked(self) -> set[str]:
        with self._lock:
            return set(self._state["revoked"])

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._state)

    def is_authorized(self, chat_id: Any) -> bool:
        with self._lock:
            return str(chat_id) in self._state["authorized"]

    def request_access(
        self, *, chat_id: Any, user_id: Any, username: str = "", display_name: str = "",
    ) -> tuple[str, dict[str, Any]]:
        chat_id = str(chat_id)
        if not chat_id:
            raise ValueError("chat_id пуст")
        with self._lock:
            if chat_id in self._state["authorized"]:
                return "authorized", deepcopy(self._state["authorized"][chat_id])
            if chat_id in self._state["pending"]:
                return "pending", deepcopy(self._state["pending"][chat_id])
            entry = {
                "chat_id": chat_id,
                "user_id": str(user_id),
                "username": str(username or ""),
                "display_name": str(display_name or ""),
                "requested_at": time.time(),
            }
            self._state["pending"][chat_id] = entry
            self._save()
            return "requested", deepcopy(entry)

    def grant(self, chat_id: Any, *, granted_by: Any = "") -> dict[str, Any]:
        chat_id = str(chat_id)
        if not chat_id:
            raise ValueError("chat_id пуст")
        with self._lock:
            entry = self._state["pending"].pop(chat_id, None)
            if entry is None:
                entry = deepcopy(self._state["authorized"].get(chat_id)) or {
                    "chat_id": chat_id,
                    "user_id": chat_id,
                    "username": "",
                    "display_name": "",
                }
            entry["granted_by"] = str(granted_by)
            entry["granted_at"] = time.time()
            self._state["authorized"][chat_id] = entry
            self._state["revoked"] = [value for value in self._state["revoked"] if value != chat_id]
            self._save()
            return deepcopy(entry)

    def revoke(self, chat_id: Any) -> bool:
        chat_id = str(chat_id)
        with self._lock:
            changed = self._state["authorized"].pop(chat_id, None) is not None
            changed = self._state["pending"].pop(chat_id, None) is not None or changed
            if changed and chat_id not in self._state["revoked"]:
                self._state["revoked"].append(chat_id)
                self._state["revoked"].sort()
            if changed:
                self._save()
            return changed

    def deny(self, chat_id: Any) -> bool:
        return self.revoke(chat_id)

    def set_setting(self, key: str, value: Any) -> None:
        if key not in SETTING_VALUES or value not in SETTING_VALUES[key]:
            raise ValueError(f"Недопустимое значение {key}={value!r}")
        with self._lock:
            self._state["settings"][key] = value
            self._save()

    def advance_offset(self, value: int) -> None:
        value = max(0, int(value))
        with self._lock:
            if value <= self._state["update_offset"]:
                return
            self._state["update_offset"] = value
            self._save()


class TelegramDetailsStore:
    """Bounded SQLite TTL cache used by the Telegram cohort callback button."""

    def __init__(self, path: str, *, max_entries: int = 500, ttl_seconds: int = 86400):
        self.path = os.path.abspath(path)
        self.max_entries = max(1, int(max_entries))
        self.ttl_seconds = max(0, int(ttl_seconds))
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS details ("
                "listing_id TEXT PRIMARY KEY, body TEXT NOT NULL, updated_at REAL NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def put(self, listing_id: Any, body: str) -> None:
        key = str(listing_id)
        if not key:
            return
        now = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO details(listing_id, body, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(listing_id) DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at",
                (key, str(body), now),
            )
            db.execute(
                "DELETE FROM details WHERE listing_id IN ("
                "SELECT listing_id FROM details ORDER BY updated_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
                (self.max_entries,),
            )

    def get(self, listing_id: Any) -> str | None:
        key = str(listing_id)
        now = time.time()
        with self._connect() as db:
            row = db.execute(
                "SELECT body, updated_at FROM details WHERE listing_id=?", (key,)
            ).fetchone()
            if row is None:
                return None
            if self.ttl_seconds == 0 or now - float(row[1]) >= self.ttl_seconds:
                db.execute("DELETE FROM details WHERE listing_id=?", (key,))
                return None
            return str(row[0])

    def count(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM details").fetchone()[0])
