"""SQLite-backed persistence for ShadowPI."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def _now_ts() -> int:
    return int(time.time())


class Database:
    """Simple SQLite helper used for user tracking and watchlists."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._setup()

    def close(self) -> None:
        self._conn.close()

    def _setup(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id     INTEGER PRIMARY KEY,
                    username    TEXT,
                    full_name   TEXT,
                    first_seen  INTEGER,
                    last_seen   INTEGER,
                    first_group INTEGER,
                    last_group  INTEGER,
                    cas_status  TEXT,
                    cas_checked INTEGER,
                    local_trust TEXT DEFAULT 'normal',
                    messages_sent INTEGER DEFAULT 0,
                    links_sent INTEGER DEFAULT 0,
                    forwards_sent INTEGER DEFAULT 0,
                    warnings INTEGER DEFAULT 0,
                    deleted_by_mod INTEGER DEFAULT 0,
                    flags TEXT,
                    newbie_until INTEGER DEFAULT 0,
                    identity_changes INTEGER DEFAULT 0,
                    first_message_ts INTEGER DEFAULT 0,
                    first_message_type TEXT,
                    first_forward_ts INTEGER DEFAULT 0,
                    shadowbanned INTEGER DEFAULT 0,
                    is_deleted INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS overrides (
                    user_id INTEGER PRIMARY KEY,
                    action TEXT NOT NULL,
                    note TEXT,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cas_watchlist (
                    user_id INTEGER PRIMARY KEY,
                    reason TEXT,
                    source TEXT,
                    added_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def _execute(self, query: str, *params: Any) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(query, params)
            self._conn.commit()
            return cur

    def record_user_seen(
        self,
        user_id: int,
        username: str | None,
        chat_id: int,
        seen_ts: int | None = None,
        *,
        full_name: str | None = None,
        is_deleted: bool = False,
    ) -> dict[str, Any]:
        seen_ts = seen_ts or _now_ts()
        row = self.fetch_user(user_id)
        if row:
            identity_changes = 0
            if username and row.get("username") and username != row.get("username"):
                identity_changes = 1
            if full_name and row.get("full_name") and full_name != row.get("full_name"):
                identity_changes = 1
            self._execute(
                """
                UPDATE users
                SET username = COALESCE(?, username),
                    full_name = COALESCE(?, full_name),
                    last_seen = ?,
                    last_group = ?,
                    identity_changes = identity_changes + ?,
                    is_deleted = ?
                WHERE user_id = ?
                """,
                username,
                full_name,
                seen_ts,
                chat_id,
                identity_changes,
                1 if is_deleted else 0,
                user_id,
            )
        else:
            self._execute(
                """
                INSERT INTO users (
                    user_id,
                    username,
                    full_name,
                    first_seen,
                    last_seen,
                    first_group,
                    last_group,
                    is_deleted
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                user_id,
                username,
                full_name,
                seen_ts,
                seen_ts,
                chat_id,
                chat_id,
                1 if is_deleted else 0,
            )
        return self.fetch_user(user_id)

    def fetch_user(self, user_id: int) -> dict[str, Any]:
        cur = self._execute("SELECT * FROM users WHERE user_id = ?", user_id)
        row = cur.fetchone()
        return dict(row) if row else {}

    def update_cas_status(self, user_id: int, status: str) -> None:
        self._execute(
            """
            UPDATE users
            SET cas_status = ?, cas_checked = ?
            WHERE user_id = ?
            """,
            status,
            _now_ts(),
            user_id,
        )

    def record_first_message(
        self,
        user_id: int,
        timestamp: int,
        message_type: str,
        forwarded: bool,
    ) -> None:
        row = self.fetch_user(user_id)
        if not row:
            return
        if row.get("first_message_ts"):
            if forwarded and not row.get("first_forward_ts"):
                self._execute(
                    "UPDATE users SET first_forward_ts = ? WHERE user_id = ?",
                    timestamp,
                    user_id,
                )
            return
        forward_ts = timestamp if forwarded else 0
        self._execute(
            """
            UPDATE users
            SET first_message_ts = ?,
                first_message_type = ?,
                first_forward_ts = COALESCE(NULLIF(?, 0), first_forward_ts)
            WHERE user_id = ?
            """,
            timestamp,
            message_type,
            forward_ts,
            user_id,
        )

    def increment_counters(
        self,
        user_id: int,
        *,
        messages: int = 0,
        links: int = 0,
        forwards: int = 0,
        warnings: int = 0,
        deletions: int = 0,
        flags: str | None = None,
    ) -> None:
        self._execute(
            """
            UPDATE users
            SET messages_sent = messages_sent + ?,
                links_sent = links_sent + ?,
                forwards_sent = forwards_sent + ?,
                warnings = warnings + ?,
                deleted_by_mod = deleted_by_mod + ?,
                flags = COALESCE(?, flags)
            WHERE user_id = ?
            """,
            messages,
            links,
            forwards,
            warnings,
            deletions,
            flags,
            user_id,
        )

    def set_newbie_until(self, user_id: int, ts: int) -> None:
        self._execute(
            "UPDATE users SET newbie_until = ? WHERE user_id = ?",
            ts,
            user_id,
        )

    def get_override(self, user_id: int) -> dict[str, Any] | None:
        cur = self._execute("SELECT * FROM overrides WHERE user_id = ?", user_id)
        row = cur.fetchone()
        return dict(row) if row else None

    def set_override(self, user_id: int, action: str, note: str | None = None) -> None:
        self._execute(
            """
            INSERT INTO overrides (user_id, action, note, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE
            SET action = excluded.action,
                note = excluded.note,
                updated_at = excluded.updated_at
            """,
            user_id,
            action,
            note,
            _now_ts(),
        )

    def clear_override(self, user_id: int) -> None:
        self._execute("DELETE FROM overrides WHERE user_id = ?", user_id)

    def set_local_trust(self, user_id: int, trust: str) -> None:
        self._execute(
            "UPDATE users SET local_trust = ? WHERE user_id = ?",
            trust,
            user_id,
        )

    def set_shadowban(self, user_id: int, enabled: bool) -> None:
        self._execute(
            "UPDATE users SET shadowbanned = ? WHERE user_id = ?",
            1 if enabled else 0,
            user_id,
        )

    def is_shadowbanned(self, user_id: int) -> bool:
        row = self.fetch_user(user_id)
        return bool(row.get("shadowbanned")) if row else False

    def upsert_watchlist(self, entries: Iterable[tuple[int, str | None]], source: str) -> int:
        added = 0
        with self._lock:
            cur = self._conn.cursor()
            now = _now_ts()
            for user_id, reason in entries:
                cur.execute(
                    """
                    INSERT INTO cas_watchlist (user_id, reason, source, added_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE
                    SET reason = excluded.reason,
                        source = excluded.source,
                        added_at = excluded.added_at
                    """,
                    (user_id, reason, source, now),
                )
                added += 1
            self._conn.commit()
        return added

    def in_watchlist(self, user_id: int) -> str | None:
        cur = self._execute("SELECT reason FROM cas_watchlist WHERE user_id = ?", user_id)
        row = cur.fetchone()
        return row["reason"] if row else None

    def watchlist_size(self) -> int:
        cur = self._execute("SELECT COUNT(*) AS total FROM cas_watchlist")
        row = cur.fetchone()
        return int(row["total"] if row else 0)

    def watchlist_ids(self) -> set[int]:
        cur = self._execute("SELECT user_id FROM cas_watchlist")
        return {int(row[0]) for row in cur.fetchall()}

    def set_state_value(self, key: str, value: str) -> None:
        self._execute(
            """
            INSERT INTO bot_state (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            key,
            value,
        )

    def get_state_value(self, key: str, default: str | None = None) -> str | None:
        cur = self._execute("SELECT value FROM bot_state WHERE key = ?", key)
        row = cur.fetchone()
        if row:
            return row["value"]
        return default

    def get_flag(self, key: str, default: bool = False) -> bool:
        value = self.get_state_value(key)
        if value is None:
            return default
        return value == "1"

    def set_flag(self, key: str, enabled: bool) -> None:
        self.set_state_value(key, "1" if enabled else "0")

    def counts_summary(self) -> dict[str, Any]:
        cur = self._execute(
            """
            SELECT COUNT(*) as total_users,
                   SUM(messages_sent) as total_messages,
                   SUM(deleted_by_mod) as total_deletes,
                   SUM(warnings) as total_warnings
            FROM users
            """
        )
        row = cur.fetchone()
        return {key: row[key] or 0 for key in row.keys()} if row else {}

    def users_by_chat(self, chat_id: int, limit: int | None = None) -> list[dict[str, Any]]:
        if limit:
            cur = self._execute(
                "SELECT * FROM users WHERE last_group = ? ORDER BY last_seen DESC LIMIT ?",
                chat_id,
                limit,
            )
        else:
            cur = self._execute(
                "SELECT * FROM users WHERE last_group = ? ORDER BY last_seen DESC",
                chat_id,
            )
        return [dict(row) for row in cur.fetchall()]
