"""SQLite persistence for the single-project edit document.

One database file in the data directory.  Uploaded blobs and extracted media
live on disk; the DB stores only their (immutable) paths plus edit state.

Every mutating request goes through :func:`connect` as a short-lived
connection inside a lock, which is plenty for a local single-user tool.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    duration TEXT NOT NULL DEFAULT '0',
    edit_version INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO project (id, duration) VALUES (1, '0');

CREATE TABLE IF NOT EXISTS assets (
    role TEXT PRIMARY KEY,
    kind TEXT NOT NULL,            -- 'video' | 'audio'
    path TEXT NOT NULL,            -- original upload (immutable)
    fps TEXT,                      -- video: rational frame rate
    frames_dir TEXT,               -- video: extracted PNGs
    frame_count INTEGER,
    width INTEGER,
    height INTEGER,
    sample_rate INTEGER,           -- audio: native rate
    channels INTEGER,
    samples INTEGER,
    pcm_path TEXT,                 -- audio: decoded float32 PCM
    uploaded_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS anchors (
    role TEXT NOT NULL,
    idx INTEGER NOT NULL,
    material TEXT NOT NULL,
    output TEXT NOT NULL,
    PRIMARY KEY (role, idx)
);

CREATE TABLE IF NOT EXISTS cuts (
    idx INTEGER PRIMARY KEY,
    start TEXT NOT NULL,
    finish TEXT NOT NULL,
    camera TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> "OwnedConn":
        return OwnedConn(self._connect(), self._lock)


class OwnedConn:
    """Connection bound to the global write lock for the duration of a call."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self.conn = conn
        self._lock = lock
        self._lock.acquire()

    def __enter__(self):
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        self.conn.close()
        self._lock.release()


def bump_edit_version(conn: sqlite3.Connection) -> int:
    cur = conn.execute("UPDATE project SET edit_version = edit_version + 1")
    assert cur.rowcount == 1
    row = conn.execute("SELECT edit_version FROM project WHERE id = 1").fetchone()
    return int(row["edit_version"])


def load_json_state(conn: sqlite3.Connection) -> dict:
    proj = conn.execute("SELECT duration, edit_version FROM project WHERE id=1").fetchone()
    raw_assets = {}
    for row in conn.execute("SELECT * FROM assets"):
        d = dict(row)
        role = d.pop("role")
        raw_assets[role] = d
    anchors: dict[str, list] = {}
    for row in conn.execute("SELECT role, material, output FROM anchors ORDER BY idx"):
        anchors.setdefault(row["role"], []).append(
            {"material": row["material"], "output": row["output"]}
        )
    cuts = [
        {"start": row["start"], "end": row["finish"], "camera": row["camera"]}
        for row in conn.execute(
            "SELECT start, finish, camera FROM cuts ORDER BY idx"
        )
    ]
    return {
        "duration": proj["duration"],
        "edit_version": proj["edit_version"],
        "assets": raw_assets,
        "anchors": anchors,
        "cuts": cuts,
    }
