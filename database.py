"""SQLite persistence layer for the Cricway Enterprise Support System."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

DB_PATH = Path(os.environ.get("CRICWAY_DB", "cricway.db"))

TICKET_PREFIX = "CRIC"
TICKET_START = 1001  # First ticket id

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    username       TEXT,
    first_name     TEXT,
    first_seen     TEXT NOT NULL,
    last_active    TEXT NOT NULL,
    total_requests INTEGER NOT NULL DEFAULT 0,
    is_admin       INTEGER NOT NULL DEFAULT 0,
    language       TEXT NOT NULL DEFAULT 'en'
);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    subject        TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'OPEN',     -- OPEN | IN_PROGRESS | RESOLVED
    priority       TEXT NOT NULL DEFAULT 'MEDIUM',   -- LOW | MEDIUM | HIGH
    handled_by     TEXT NOT NULL DEFAULT 'PENDING', -- AI | ADMIN | PENDING
    assigned_admin INTEGER,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ticket_replies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL,
    sender_id   INTEGER,
    sender_role TEXT NOT NULL,                    -- USER | ADMIN | AI | SYSTEM
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id)
);

CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT NOT NULL,                     -- INFO | WARN | ERROR
    category   TEXT NOT NULL,                     -- USER | ADMIN | AI | SYSTEM | BROADCAST
    actor_id   INTEGER,
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_tickets_status  ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_user    ON tickets(user_id);
CREATE INDEX IF NOT EXISTS idx_tickets_updated ON tickets(updated_at);
CREATE INDEX IF NOT EXISTS idx_replies_ticket  ON ticket_replies(ticket_id);
CREATE INDEX IF NOT EXISTS idx_logs_created    ON logs(created_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    try:
        yield con
    finally:
        con.close()


def init_db() -> None:
    with connect() as con:
        con.executescript(SCHEMA)
        # Lightweight migration: add columns introduced after first release
        existing_cols = {row["name"] for row in con.execute("PRAGMA table_info(users)")}
        if "first_name" not in existing_cols:
            con.execute("ALTER TABLE users ADD COLUMN first_name TEXT")
        # Seed ticket auto-increment so the first ticket gets id = TICKET_START
        cur = con.execute("SELECT seq FROM sqlite_sequence WHERE name='tickets'")
        row = cur.fetchone()
        if row is None:
            con.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES ('tickets', ?)",
                (TICKET_START - 1,),
            )
        # Default settings
        con.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('ai_enabled', '1')"
        )
        con.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('ai_model', ?)",
            (os.environ.get("AI_MODEL", "gemini-2.5-flash"),),
        )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with connect() as con:
        row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------------------
# Logging (DB-side, separate from python logging)
# ---------------------------------------------------------------------------


def log_event(level: str, category: str, message: str, actor_id: Optional[int] = None) -> None:
    try:
        with connect() as con:
            con.execute(
                "INSERT INTO logs (level, category, actor_id, message, created_at) VALUES (?, ?, ?, ?, ?)",
                (level, category, actor_id, message, now_iso()),
            )
    except sqlite3.Error:
        pass  # never let logging crash the bot


def fetch_logs(limit: int = 30) -> list[sqlite3.Row]:
    with connect() as con:
        return list(
            con.execute(
                "SELECT level, category, actor_id, message, created_at "
                "FROM logs ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        )


# ---------------------------------------------------------------------------
# Ticket id helpers
# ---------------------------------------------------------------------------


def format_ticket_id(numeric: int) -> str:
    return f"{TICKET_PREFIX}-{numeric}"


def parse_ticket_id(value: str) -> Optional[int]:
    if not value:
        return None
    s = value.strip().upper()
    if s.startswith(f"{TICKET_PREFIX}-"):
        s = s[len(TICKET_PREFIX) + 1 :]
    return int(s) if s.isdigit() else None
