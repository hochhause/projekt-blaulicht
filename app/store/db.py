"""SQLite-backed store. Synchronous, stdlib-only, fully unit-testable.

One :class:`Store` wraps a connection and exposes small, explicit methods —
no ORM. Credentials are stored **sealed** (see :mod:`app.core.secrets`); this
layer only moves opaque bytes and never sees plaintext passwords.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .models import Account

_SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    label      TEXT NOT NULL,
    email      TEXT NOT NULL,
    server     TEXT NOT NULL DEFAULT 'de',
    enabled    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS secret (
    account_id INTEGER PRIMARY KEY REFERENCES account(id) ON DELETE CASCADE,
    token      BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS config_profile (
    account_id INTEGER PRIMARY KEY REFERENCES account(id) ON DELETE CASCADE,
    json       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_state (
    account_id INTEGER PRIMARY KEY REFERENCES account(id) ON DELETE CASCADE,
    status     TEXT NOT NULL DEFAULT 'stopped',
    last_cycle TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS stat (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    ts         TEXT NOT NULL DEFAULT (datetime('now')),
    dispatched INTEGER NOT NULL DEFAULT 0,
    missions   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS app_setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    """Synchronous SQLite persistence."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        # The web handlers run in a threadpool and the orchestrator in the event
        # loop, so the connection is shared across threads (check_same_thread=
        # False) and every access is serialised by a lock. Fine for a low-traffic
        # single-operator panel; it keeps the store simple and correct.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- low-level helpers (all locked) ------------------------------------
    def _write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # -- accounts ----------------------------------------------------------
    def add_account(self, label: str, email: str, server: str = "de") -> int:
        cur = self._write(
            "INSERT INTO account (label, email, server) VALUES (?, ?, ?)",
            (label, email, server),
        )
        return int(cur.lastrowid)

    def get_account(self, account_id: int) -> Account | None:
        row = self._one("SELECT * FROM account WHERE id = ?", (account_id,))
        return _to_account(row) if row else None

    def list_accounts(self) -> list[Account]:
        return [_to_account(r) for r in self._all("SELECT * FROM account ORDER BY id")]

    def set_enabled(self, account_id: int, enabled: bool) -> None:
        self._write(
            "UPDATE account SET enabled = ? WHERE id = ?", (1 if enabled else 0, account_id)
        )

    def update_account(self, account_id: int, *, label: str, email: str) -> None:
        self._write(
            "UPDATE account SET label = ?, email = ? WHERE id = ?",
            (label, email, account_id),
        )

    def delete_account(self, account_id: int) -> None:
        self._write("DELETE FROM account WHERE id = ?", (account_id,))

    # -- secrets (opaque sealed bytes) -------------------------------------
    def set_secret(self, account_id: int, token: bytes) -> None:
        self._write(
            "INSERT INTO secret (account_id, token) VALUES (?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET token = excluded.token",
            (account_id, token),
        )

    def get_secret(self, account_id: int) -> bytes | None:
        row = self._one("SELECT token FROM secret WHERE account_id = ?", (account_id,))
        return bytes(row["token"]) if row else None

    # -- config profile (JSON blob) ----------------------------------------
    def set_config(self, account_id: int, json_text: str) -> None:
        self._write(
            "INSERT INTO config_profile (account_id, json) VALUES (?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET json = excluded.json",
            (account_id, json_text),
        )

    def get_config(self, account_id: int) -> str | None:
        row = self._one("SELECT json FROM config_profile WHERE account_id = ?", (account_id,))
        return row["json"] if row else None

    # -- run state ---------------------------------------------------------
    def set_state(self, account_id: int, status: str, *, error: str | None = None) -> None:
        self._write(
            "INSERT INTO run_state (account_id, status, last_cycle, last_error) "
            "VALUES (?, ?, datetime('now'), ?) "
            "ON CONFLICT(account_id) DO UPDATE SET "
            "status = excluded.status, last_cycle = excluded.last_cycle, "
            "last_error = excluded.last_error",
            (account_id, status, error),
        )

    def get_state(self, account_id: int) -> dict | None:
        row = self._one("SELECT * FROM run_state WHERE account_id = ?", (account_id,))
        return dict(row) if row else None

    # -- stats -------------------------------------------------------------
    def add_stat(self, account_id: int, dispatched: int, missions: int) -> None:
        self._write(
            "INSERT INTO stat (account_id, dispatched, missions) VALUES (?, ?, ?)",
            (account_id, dispatched, missions),
        )

    def totals(self, account_id: int) -> dict:
        row = self._one(
            "SELECT COALESCE(SUM(dispatched), 0) AS dispatched, "
            "COALESCE(SUM(missions), 0) AS missions FROM stat WHERE account_id = ?",
            (account_id,),
        )
        return dict(row)

    # -- app settings ------------------------------------------------------
    def set_setting(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO app_setting (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_setting(self, key: str) -> str | None:
        row = self._one("SELECT value FROM app_setting WHERE key = ?", (key,))
        return row["value"] if row else None


def _to_account(row: sqlite3.Row) -> Account:
    return Account(
        id=row["id"],
        label=row["label"],
        email=row["email"],
        server=row["server"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
    )
