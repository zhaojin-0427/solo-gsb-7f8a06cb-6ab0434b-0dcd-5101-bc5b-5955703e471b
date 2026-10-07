"""SQLite persistence for projects, assets, edits and exports.

An export binds a deep JSON SNAPSHOT taken at start: even if the user keeps
editing afterwards, the render reads ``export_jobs.snapshot`` (and the
asset file paths snapshotted into it), never the live edit.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    kind TEXT NOT NULL CHECK (kind IN ('video','audio')),
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    uploaded_at REAL NOT NULL,
    -- video
    width INTEGER, height INTEGER, fps_num INTEGER, fps_den INTEGER,
    nframes INTEGER,
    -- audio
    channels INTEGER, in_sample_rate INTEGER, nsamples INTEGER,
    -- shared clock model: displayed timestamp = material0 + clock*wall_time
    material0 TEXT NOT NULL DEFAULT '0',
    clock_num INTEGER NOT NULL DEFAULT 1,
    clock_den INTEGER NOT NULL DEFAULT 1,
    material_span TEXT NOT NULL DEFAULT '0'
);
CREATE TABLE IF NOT EXISTS edits (
    project_id INTEGER PRIMARY KEY REFERENCES projects(id),
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS export_jobs (
    id TEXT PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    status TEXT NOT NULL CHECK (status IN ('running','done','failed','canceled')),
    snapshot TEXT NOT NULL,
    output_path TEXT,
    mapping_path TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
"""

_lock = threading.RLock()


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init(conn: sqlite3.Connection) -> None:
    with _lock:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT id FROM projects WHERE id=1").fetchone()
        if row is None:
            conn.execute("INSERT INTO projects (id, created_at) VALUES (1, ?)",
                         (time.time(),))
        conn.commit()


def create_asset(conn: sqlite3.Connection, project_id: int, kind: str,
                 filename: str, path: str, meta: dict) -> int:
    with _lock:
        cur = conn.execute(
            """INSERT INTO assets
               (project_id, kind, filename, path, uploaded_at,
                width, height, fps_num, fps_den, nframes,
                channels, in_sample_rate, nsamples,
                material0, clock_num, clock_den, material_span)
               VALUES (?,?,?,?,?,?, ?,?,?,?, ?,?,?, ?,?,?,?)""",
            (project_id, kind, filename, path, time.time(),
             meta.get("width"), meta.get("height"),
             meta.get("fps_num"), meta.get("fps_den"), meta.get("nframes"),
             meta.get("channels"), meta.get("in_sample_rate"),
             meta.get("nsamples"),
             str(meta.get("material0", 0)),
             int(meta.get("clock_num", 1)), int(meta.get("clock_den", 1)),
             str(meta.get("material_span", 0))),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_assets(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM assets WHERE project_id=? ORDER BY id", (project_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_asset(conn: sqlite3.Connection, asset_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    return dict(r) if r else None


def update_asset_clock(conn: sqlite3.Connection, asset_id: int,
                       material0: str, clock_num: int, clock_den: int) -> None:
    with _lock:
        conn.execute(
            "UPDATE assets SET material0=?, clock_num=?, clock_den=? WHERE id=?",
            (material0, clock_num, clock_den, asset_id),
        )
        conn.commit()


def save_edit(conn: sqlite3.Connection, project_id: int, payload: dict) -> None:
    with _lock:
        conn.execute(
            """INSERT INTO edits (project_id, payload, updated_at)
               VALUES (?,?,?)
               ON CONFLICT(project_id) DO UPDATE SET
                 payload=excluded.payload, updated_at=excluded.updated_at""",
            (project_id, json.dumps(payload, ensure_ascii=False), time.time()),
        )
        conn.commit()


def load_edit(conn: sqlite3.Connection, project_id: int) -> dict | None:
    r = conn.execute("SELECT payload FROM edits WHERE project_id=?",
                     (project_id,)).fetchone()
    return json.loads(r["payload"]) if r else None


def make_snapshot(conn: sqlite3.Connection, project_id: int,
                  edit_payload: dict) -> dict:
    """Deep, immutable snapshot bound to an export."""
    return {
        "project_id": project_id,
        "captured_at": time.time(),
        "nonce": uuid.uuid4().hex,
        "edit": edit_payload,
        "assets": list_assets(conn, project_id),
    }


def create_export(conn: sqlite3.Connection, project_id: int,
                  snapshot: dict) -> str:
    job_id = uuid.uuid4().hex
    with _lock:
        conn.execute(
            """INSERT INTO export_jobs
               (id, project_id, status, snapshot, created_at)
               VALUES (?,?, 'running', ?, ?)""",
            (job_id, project_id, json.dumps(snapshot), time.time()),
        )
        conn.commit()
    return job_id


def get_export(conn: sqlite3.Connection, job_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM export_jobs WHERE id=?",
                     (job_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["snapshot_obj"] = json.loads(d.pop("snapshot"))
    return d


def claim_terminal(conn: sqlite3.Connection, job_id: str,
                   status: str, output_path: str | None = None,
                   mapping_path: str | None = None,
                   error: str | None = None) -> bool:
    """Atomically move running -> one terminal state.

    Returns True only for the single winner.  A late cancel-vs-complete race
    can never produce two terminal states; the loser publishes nothing.
    """
    with _lock:
        cur = conn.execute(
            """UPDATE export_jobs
               SET status=?, output_path=COALESCE(?, output_path),
                   mapping_path=COALESCE(?, mapping_path),
                   error=?, finished_at=?
               WHERE id=? AND status='running'""",
            (status, output_path, mapping_path, error, time.time(), job_id),
        )
        conn.commit()
        return cur.rowcount == 1


def list_exports(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, status, output_path, mapping_path, error, created_at, finished_at "
        "FROM export_jobs WHERE project_id=? ORDER BY created_at DESC",
        (project_id,),
    ).fetchall()
    return [dict(r) for r in rows]
