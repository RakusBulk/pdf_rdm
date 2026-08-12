from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "drm.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id             TEXT PRIMARY KEY,
    title              TEXT NOT NULL,
    key_hex            TEXT NOT NULL,
    original_filename  TEXT,
    file_size          INTEGER,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS licenses (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id              TEXT NOT NULL REFERENCES documents(doc_id),
    machine_fingerprint TEXT NOT NULL,
    label               TEXT,
    expires_at          TEXT NOT NULL,
    revoked             INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(doc_id, machine_fingerprint)
);

CREATE TABLE IF NOT EXISTS access_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id              TEXT NOT NULL,
    machine_fingerprint TEXT NOT NULL,
    result              TEXT NOT NULL,
    at                  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pending_requests (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_fingerprint TEXT NOT NULL,
    username            TEXT,
    email               TEXT NOT NULL,
    note                TEXT,
    status              TEXT NOT NULL DEFAULT 'pending',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at          TEXT
);

CREATE TABLE IF NOT EXISTS groups (
    group_id    TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS group_documents (
    group_id    TEXT NOT NULL REFERENCES groups(group_id),
    doc_id      TEXT NOT NULL REFERENCES documents(doc_id),
    PRIMARY KEY (group_id, doc_id)
);

CREATE TABLE IF NOT EXISTS admin_users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    session_token TEXT PRIMARY KEY,
    username      TEXT NOT NULL REFERENCES admin_users(username),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at    TEXT NOT NULL
);
"""


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    # Idempotent migrations for columns added after initial release --
    # CREATE TABLE IF NOT EXISTS above doesn't touch already-existing tables.
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(pending_requests)").fetchall()}
    if "username" not in cols:
        conn.execute("ALTER TABLE pending_requests ADD COLUMN username TEXT")

    doc_cols = {row["name"] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
    if "original_filename" not in doc_cols:
        conn.execute("ALTER TABLE documents ADD COLUMN original_filename TEXT")
    if "file_size" not in doc_cols:
        conn.execute("ALTER TABLE documents ADD COLUMN file_size INTEGER")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
