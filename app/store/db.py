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
    missions   INTEGER NOT NULL DEFAULT 0,
    credits    INTEGER NOT NULL DEFAULT 0
);
-- Cumulative tally of how often each vehicle class fell short / struck out per
-- account (the "vehicles we struggle to field" list on the dashboard).
CREATE TABLE IF NOT EXISTS shortfall (
    account_id    INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    vehicle_class TEXT NOT NULL,
    count         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, vehicle_class)
);
CREATE TABLE IF NOT EXISTS app_setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Live red/yellow/green/unknown breakdown of the CURRENT mission feed, per
-- account — a snapshot overwritten every cycle (unlike shortfall, which is
-- cumulative). Backs the dashboard's live mission-status display.
CREATE TABLE IF NOT EXISTS mission_status (
    account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    status     TEXT NOT NULL,
    count      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, status)
);
-- Per-mission bot verdict for the panel's "mission breakdown" view, per
-- account — a snapshot overwritten every cycle (same lifecycle as
-- mission_status, unlike the cumulative shortfall table).
CREATE TABLE IF NOT EXISTS mission_breakdown (
    account_id  INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    mission_id  INTEGER NOT NULL,
    caption     TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL,
    live_status TEXT NOT NULL,
    bot_status  TEXT NOT NULL,
    explanation TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account_id, mission_id)
);
-- Last time we actually dispatched >=1 vehicle to a mission, per account --
-- UNLIKE mission_breakdown, this ACCUMULATES across cycles (a mission's
-- last-interaction time must survive cycles where nothing new was sent to
-- it); pruned per-account each cycle for missions no longer in the live
-- feed, so it can't grow unbounded over the account's lifetime.
CREATE TABLE IF NOT EXISTS mission_interaction (
    account_id        INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    mission_id        INTEGER NOT NULL,
    last_interacted_at REAL NOT NULL,
    PRIMARY KEY (account_id, mission_id)
);
-- How many missions closed after N real alarms (initial send / reinforcement,
-- never a parasite send or S5 handling -- see AlarmTracker), per account and
-- per N. Cumulative like `shortfall`, but cleared on every account (re)start
-- (Sasha, 2026-10-27: "doesn't need to survive a restart") -- same "DB-
-- stored, per-run" pattern as `shortfall`, not a long-run historical record.
CREATE TABLE IF NOT EXISTS alarm_count (
    account_id  INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    alarm_count INTEGER NOT NULL,
    missions    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, alarm_count)
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
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created (existing installs
        already have the tables; ``CREATE IF NOT EXISTS`` won't add new columns)."""
        with self._lock:
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(stat)")}
            if "credits" not in cols:
                self._conn.execute(
                    "ALTER TABLE stat ADD COLUMN credits INTEGER NOT NULL DEFAULT 0"
                )
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
    def add_stat(self, account_id: int, dispatched: int, missions: int, credits: int = 0) -> None:
        self._write(
            "INSERT INTO stat (account_id, dispatched, missions, credits) VALUES (?, ?, ?, ?)",
            (account_id, dispatched, missions, credits),
        )

    def totals(self, account_id: int) -> dict:
        row = self._one(
            "SELECT COALESCE(SUM(dispatched), 0) AS dispatched, "
            "COALESCE(SUM(missions), 0) AS missions, "
            "COALESCE(SUM(credits), 0) AS credits FROM stat WHERE account_id = ?",
            (account_id,),
        )
        return dict(row)

    def stat_series(self, account_id: int | None = None, hours: int = 24) -> list[dict]:
        """Hourly buckets of dispatched + credits, oldest→newest, for the chart.

        *account_id* ``None`` combines all accounts.
        """
        if account_id is None:
            rows = self._all(
                "SELECT strftime('%Y-%m-%d %H', ts) AS bucket, "
                "COALESCE(SUM(dispatched), 0) AS dispatched, "
                "COALESCE(SUM(credits), 0) AS credits "
                "FROM stat GROUP BY bucket ORDER BY bucket DESC LIMIT ?",
                (hours,),
            )
        else:
            rows = self._all(
                "SELECT strftime('%Y-%m-%d %H', ts) AS bucket, "
                "COALESCE(SUM(dispatched), 0) AS dispatched, "
                "COALESCE(SUM(credits), 0) AS credits "
                "FROM stat WHERE account_id = ? GROUP BY bucket ORDER BY bucket DESC LIMIT ?",
                (account_id, hours),
            )
        return [dict(r) for r in reversed(rows)]

    # -- shortfalls (struggling-to-field tally) ----------------------------
    def add_shortfalls(self, account_id: int, counts) -> None:
        """Increment the per-class shortfall tally. *counts* maps class → count."""
        for vehicle_class, n in dict(counts).items():
            if not n:
                continue
            self._write(
                "INSERT INTO shortfall (account_id, vehicle_class, count) VALUES (?, ?, ?) "
                "ON CONFLICT(account_id, vehicle_class) DO UPDATE SET "
                "count = count + excluded.count",
                (account_id, str(vehicle_class), int(n)),
            )

    def shortfalls(self, account_id: int, limit: int = 12) -> list[dict]:
        rows = self._all(
            "SELECT vehicle_class, count FROM shortfall WHERE account_id = ? "
            "ORDER BY count DESC, vehicle_class LIMIT ?",
            (account_id, limit),
        )
        return [dict(r) for r in rows]

    def clear_shortfalls(self, account_id: int) -> None:
        """Wipe the shortfall tally — called on account (re)start so the
        'struggling to field' list reflects the current run, like strikes."""
        self._write("DELETE FROM shortfall WHERE account_id = ?", (account_id,))

    # -- live mission-status snapshot ---------------------------------------
    def set_mission_status(self, account_id: int, counts) -> None:
        """Overwrite the live status snapshot for an account — the CURRENT
        cycle's breakdown, not a cumulative tally (see `shortfall` for that
        pattern). *counts* maps status ("red"/"yellow"/"green"/"unknown") →
        count; zero/falsy entries are dropped rather than stored as zero rows."""
        with self._lock:
            self._conn.execute("DELETE FROM mission_status WHERE account_id = ?", (account_id,))
            for status, n in dict(counts).items():
                if not n:
                    continue
                self._conn.execute(
                    "INSERT INTO mission_status (account_id, status, count) VALUES (?, ?, ?)",
                    (account_id, str(status), int(n)),
                )
            self._conn.commit()

    def mission_status(self, account_id: int) -> dict[str, int]:
        rows = self._all(
            "SELECT status, count FROM mission_status WHERE account_id = ?", (account_id,)
        )
        return {r["status"]: r["count"] for r in rows}

    # -- mission breakdown snapshot ------------------------------------------
    def set_mission_breakdown(self, account_id: int, rows) -> None:
        """Overwrite the mission-breakdown snapshot for an account — the
        CURRENT cycle's per-mission bot verdict, not a cumulative tally
        (same "overwrite every cycle" pattern as `set_mission_status`).
        *rows* is an iterable of dicts with mission_id/caption/category/
        live_status/bot_status/explanation (see DispatchRunner._mission_breakdown)."""
        with self._lock:
            self._conn.execute("DELETE FROM mission_breakdown WHERE account_id = ?", (account_id,))
            for r in rows:
                self._conn.execute(
                    "INSERT INTO mission_breakdown "
                    "(account_id, mission_id, caption, category, live_status, bot_status, explanation) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (account_id, int(r["mission_id"]), str(r.get("caption", "")),
                     str(r["category"]), str(r["live_status"]), str(r["bot_status"]),
                     str(r.get("explanation", ""))),
                )
            self._conn.commit()

    def mission_breakdown(self, account_id: int) -> list[dict]:
        rows = self._all(
            "SELECT mission_id, caption, category, live_status, bot_status, explanation "
            "FROM mission_breakdown WHERE account_id = ? ORDER BY mission_id",
            (account_id,),
        )
        return [dict(r) for r in rows]

    # -- mission interaction timestamps ---------------------------------------
    def record_mission_interactions(self, account_id: int, interactions, live_mission_ids) -> None:
        """Upsert *interactions* (mission_id -> unix timestamp of the last
        cycle we sent it >=1 vehicle), then prune any recorded mission no
        longer in *live_mission_ids* (this cycle's full considered set) --
        once a mission leaves the feed it'll never need "time since" shown
        again, so keeping its row forever would grow this table unbounded."""
        with self._lock:
            for mission_id, ts in dict(interactions).items():
                self._conn.execute(
                    "INSERT INTO mission_interaction (account_id, mission_id, last_interacted_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(account_id, mission_id) "
                    "DO UPDATE SET last_interacted_at = excluded.last_interacted_at",
                    (account_id, int(mission_id), float(ts)),
                )
            live_ids = list(live_mission_ids)
            if live_ids:
                placeholders = ",".join("?" for _ in live_ids)
                self._conn.execute(
                    f"DELETE FROM mission_interaction WHERE account_id = ? "
                    f"AND mission_id NOT IN ({placeholders})",
                    (account_id, *live_ids),
                )
            else:
                self._conn.execute(
                    "DELETE FROM mission_interaction WHERE account_id = ?", (account_id,)
                )
            self._conn.commit()

    def mission_interactions(self, account_id: int) -> dict[int, float]:
        rows = self._all(
            "SELECT mission_id, last_interacted_at FROM mission_interaction WHERE account_id = ?",
            (account_id,),
        )
        return {r["mission_id"]: r["last_interacted_at"] for r in rows}

    # -- alarm-count distribution (sends-per-closed-mission tally) ----------
    def add_alarm_counts(self, account_id: int, counts) -> None:
        """Fold newly-closed missions into the per-account distribution.
        *counts* maps alarm_count -> how many missions just closed with that
        many real sends (see AlarmTracker.finalize)."""
        for n, missions in dict(counts).items():
            if not missions:
                continue
            self._write(
                "INSERT INTO alarm_count (account_id, alarm_count, missions) VALUES (?, ?, ?) "
                "ON CONFLICT(account_id, alarm_count) DO UPDATE SET "
                "missions = missions + excluded.missions",
                (account_id, int(n), int(missions)),
            )

    def alarm_counts(self, account_id: int) -> list[dict]:
        rows = self._all(
            "SELECT alarm_count, missions FROM alarm_count WHERE account_id = ? "
            "ORDER BY alarm_count",
            (account_id,),
        )
        return [dict(r) for r in rows]

    def clear_alarm_counts(self, account_id: int) -> None:
        """Wipe the alarm-count distribution — called on account (re)start,
        mirroring clear_shortfalls: in-memory semantics (resets on restart),
        not a long-run historical record."""
        self._write("DELETE FROM alarm_count WHERE account_id = ?", (account_id,))

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

    # -- group (symbiotic) config ------------------------------------------
    def set_group_config(self, json_text: str) -> None:
        self.set_setting("group_config", json_text)

    def get_group_config(self) -> str | None:
        return self.get_setting("group_config")


def _to_account(row: sqlite3.Row) -> Account:
    return Account(
        id=row["id"],
        label=row["label"],
        email=row["email"],
        server=row["server"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
    )
