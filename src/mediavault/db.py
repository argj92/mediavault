"""SQLite-backed index of every tracked file across every configured root.

This is per-machine local state (see config.db_path) — it is never committed
to git and never synced between machines. Each machine rebuilds/maintains its
own index by scanning its own roots.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS roots (
    label TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    role TEXT NOT NULL,
    mode TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS worker_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- A snapshot of another machine's index, imported from a portable JSON
-- catalog (see catalog.py) so cross-machine "how many protective copies
-- does this file have" can be computed without both machines being mounted
-- at once. Re-importing the same machine name replaces its old snapshot.
CREATE TABLE IF NOT EXISTS remote_catalogs (
    machine TEXT PRIMARY KEY,
    imported_at REAL NOT NULL,
    exported_at TEXT
);

CREATE TABLE IF NOT EXISTS remote_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine TEXT NOT NULL REFERENCES remote_catalogs(machine) ON DELETE CASCADE,
    root_label TEXT NOT NULL,
    role TEXT NOT NULL,
    mode TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    size INTEGER,
    hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_remote_files_hash ON remote_files(hash);
CREATE INDEX IF NOT EXISTS idx_remote_files_machine ON remote_files(machine);

-- A file present on a backup/mirror root but never seen on primary is a
-- promotion candidate (see sync.plan_sync's copy_to_a). Ignoring one here is
-- permanent ("cancel once and for all" from the Promote section) -- it's
-- keyed on primary-relative rel_path alone since primary is singular, so it
-- suppresses the suggestion regardless of which backup root it keeps
-- turning up in.
CREATE TABLE IF NOT EXISTS promotion_ignores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_path TEXT NOT NULL UNIQUE,
    ignored_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_root TEXT NOT NULL,
    original_rel_path TEXT NOT NULL,
    quarantined_path TEXT NOT NULL,
    size INTEGER,
    reason TEXT,
    quarantined_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_label TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    size INTEGER,
    mtime REAL,
    hash TEXT,
    is_placeholder INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    title_guess TEXT,
    year_guess INTEGER,
    media_type_guess TEXT,
    UNIQUE(root_label, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash);
CREATE INDEX IF NOT EXISTS idx_files_root ON files(root_label);

CREATE TABLE IF NOT EXISTS metadata_cache (
    cache_key TEXT PRIMARY KEY,
    title_guess TEXT,
    year_guess INTEGER,
    media_type TEXT,
    tmdb_id INTEGER,
    tmdb_title TEXT,
    tmdb_year INTEGER,
    queried_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_label TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    files_scanned INTEGER DEFAULT 0,
    new_files INTEGER DEFAULT 0,
    updated_files INTEGER DEFAULT 0,
    missing_files INTEGER DEFAULT 0,
    placeholders INTEGER DEFAULT 0
);
"""


@contextmanager
def connect(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Migration: sync_pairs (manual pairwise wiring) was replaced by a single
    # primary-root basis that every other root syncs against automatically.
    conn.execute("DROP TABLE IF EXISTS sync_pairs")


def upsert_root(conn: sqlite3.Connection, label: str, path: str, role: str, mode: str, enabled: bool = True) -> None:
    conn.execute(
        """INSERT INTO roots (label, path, role, mode, enabled, created_at) VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(label) DO UPDATE SET path=excluded.path, role=excluded.role, mode=excluded.mode""",
        (label, path, role, mode, int(enabled), time.time()),
    )


def list_roots(conn: sqlite3.Connection, enabled_only: bool = False) -> list[sqlite3.Row]:
    q = "SELECT * FROM roots"
    if enabled_only:
        q += " WHERE enabled=1"
    q += " ORDER BY label"
    return conn.execute(q).fetchall()


def get_root(conn: sqlite3.Connection, label: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM roots WHERE label=?", (label,)).fetchone()


def set_root_enabled(conn: sqlite3.Connection, label: str, enabled: bool) -> None:
    conn.execute("UPDATE roots SET enabled=? WHERE label=?", (int(enabled), label))


def update_root_role_mode(conn: sqlite3.Connection, label: str, role: str, mode: str) -> None:
    """Changes what a root MEANS (role/mode) without touching its path or
    losing its indexed files — unlike remove+re-add, which wipes the index."""
    conn.execute("UPDATE roots SET role=?, mode=? WHERE label=?", (role, mode, label))


def remove_root(conn: sqlite3.Connection, label: str) -> None:
    conn.execute("DELETE FROM files WHERE root_label=?", (label,))
    conn.execute("DELETE FROM scans WHERE root_label=?", (label,))
    conn.execute("DELETE FROM roots WHERE label=?", (label,))


def get_primary_root(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The single basis every other root syncs against. There's meant to be
    at most one — add_root/upsert_root callers should check
    count_primary_roots() first and refuse a second one."""
    return conn.execute(
        "SELECT * FROM roots WHERE role='primary' AND enabled=1 ORDER BY created_at LIMIT 1"
    ).fetchone()


def count_primary_roots(conn: sqlite3.Connection, exclude_label: str | None = None) -> int:
    q = "SELECT COUNT(*) as n FROM roots WHERE role='primary' AND enabled=1"
    params: list = []
    if exclude_label:
        q += " AND label != ?"
        params.append(exclude_label)
    return conn.execute(q, params).fetchone()["n"]


def syncable_roots(conn: sqlite3.Connection, exclude_label: str) -> list[sqlite3.Row]:
    """Every other enabled root that should be compared against the primary
    (mirror = full backup, subset = intentionally partial). `watch`-mode
    roots (inbox folders) are never sync targets."""
    return conn.execute(
        "SELECT * FROM roots WHERE enabled=1 AND label != ? AND mode IN ('mirror', 'subset') ORDER BY label",
        (exclude_label,),
    ).fetchall()


def ignore_promotion(conn: sqlite3.Connection, rel_path: str) -> None:
    conn.execute(
        "INSERT INTO promotion_ignores (rel_path, ignored_at) VALUES (?, ?) "
        "ON CONFLICT(rel_path) DO NOTHING",
        (rel_path, time.time()),
    )


def unignore_promotion(conn: sqlite3.Connection, ignore_id: int) -> None:
    conn.execute("DELETE FROM promotion_ignores WHERE id=?", (ignore_id,))


def list_ignored_promotions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM promotion_ignores ORDER BY rel_path").fetchall()


def ignored_promotion_paths(conn: sqlite3.Connection) -> frozenset[str]:
    return frozenset(r["rel_path"] for r in conn.execute("SELECT rel_path FROM promotion_ignores").fetchall())


def save_remote_catalog(conn: sqlite3.Connection, machine: str, exported_at: str | None, files: list[dict]) -> None:
    """Replaces any previous snapshot for this machine name with a fresh one."""
    conn.execute("DELETE FROM remote_files WHERE machine=?", (machine,))
    conn.execute(
        """INSERT INTO remote_catalogs (machine, imported_at, exported_at) VALUES (?, ?, ?)
           ON CONFLICT(machine) DO UPDATE SET imported_at=excluded.imported_at, exported_at=excluded.exported_at""",
        (machine, time.time(), exported_at),
    )
    conn.executemany(
        """INSERT INTO remote_files (machine, root_label, role, mode, rel_path, size, hash)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            (machine, f["root_label"], f["role"], f["mode"], f["rel_path"], f.get("size"), f.get("hash"))
            for f in files
            if f.get("hash")
        ],
    )


def list_remote_catalogs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT rc.*, COUNT(rf.id) as file_count FROM remote_catalogs rc
           LEFT JOIN remote_files rf ON rf.machine = rc.machine
           GROUP BY rc.machine ORDER BY rc.machine"""
    ).fetchall()


def remove_remote_catalog(conn: sqlite3.Connection, machine: str) -> None:
    conn.execute("DELETE FROM remote_files WHERE machine=?", (machine,))
    conn.execute("DELETE FROM remote_catalogs WHERE machine=?", (machine,))


def get_worker_state(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM worker_state WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    import json

    return json.loads(row["value"])


def set_worker_state(conn: sqlite3.Connection, key: str, value) -> None:
    import json

    conn.execute(
        """INSERT INTO worker_state (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (key, json.dumps(value)),
    )


def start_scan(conn: sqlite3.Connection, root_label: str) -> int:
    cur = conn.execute(
        "INSERT INTO scans (root_label, started_at) VALUES (?, ?)",
        (root_label, time.time()),
    )
    return cur.lastrowid


def finish_scan(conn: sqlite3.Connection, scan_id: int, stats: dict) -> None:
    conn.execute(
        """UPDATE scans SET finished_at=?, files_scanned=?, new_files=?, updated_files=?,
           missing_files=?, placeholders=? WHERE id=?""",
        (
            time.time(),
            stats.get("files_scanned", 0),
            stats.get("new_files", 0),
            stats.get("updated_files", 0),
            stats.get("missing_files", 0),
            stats.get("placeholders", 0),
            scan_id,
        ),
    )


def get_file(conn: sqlite3.Connection, root_label: str, rel_path: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM files WHERE root_label=? AND rel_path=?", (root_label, rel_path)
    ).fetchone()


def get_file_by_id(conn: sqlite3.Connection, file_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()


def upsert_file(
    conn: sqlite3.Connection,
    root_label: str,
    rel_path: str,
    size: int | None,
    mtime: float | None,
    hash_: str | None,
    is_placeholder: bool,
) -> None:
    now = time.time()
    existing = get_file(conn, root_label, rel_path)
    if existing is None:
        conn.execute(
            """INSERT INTO files (root_label, rel_path, size, mtime, hash, is_placeholder,
               missing, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (root_label, rel_path, size, mtime, hash_, int(is_placeholder), now, now),
        )
    else:
        conn.execute(
            """UPDATE files SET size=?, mtime=?, hash=?, is_placeholder=?, missing=0, last_seen=?
               WHERE id=?""",
            (size, mtime, hash_, int(is_placeholder), now, existing["id"]),
        )


def mark_missing(conn: sqlite3.Connection, root_label: str, scan_started_at: float) -> int:
    """Any file not touched (last_seen) since this scan began is no longer present."""
    cur = conn.execute(
        "UPDATE files SET missing=1 WHERE root_label=? AND last_seen<? AND missing=0",
        (root_label, scan_started_at),
    )
    return cur.rowcount


def all_files(conn: sqlite3.Connection, root_label: str | None = None, include_missing: bool = False):
    q = "SELECT * FROM files WHERE 1=1"
    params: list = []
    if root_label:
        q += " AND root_label=?"
        params.append(root_label)
    if not include_missing:
        q += " AND missing=0"
    return conn.execute(q, params).fetchall()


def duplicate_groups(conn: sqlite3.Connection):
    rows = conn.execute(
        """SELECT hash, COUNT(*) as n, SUM(size) as total_size
           FROM files WHERE missing=0 AND is_placeholder=0 AND hash IS NOT NULL
           GROUP BY hash HAVING COUNT(*) > 1
           ORDER BY (COUNT(*) - 1) * MAX(size) DESC"""
    ).fetchall()
    groups = []
    for row in rows:
        files = conn.execute(
            "SELECT * FROM files WHERE hash=? AND missing=0 AND is_placeholder=0", (row["hash"],)
        ).fetchall()
        groups.append({"hash": row["hash"], "count": row["n"], "total_size": row["total_size"], "files": files})
    return groups


def get_cached_metadata(conn: sqlite3.Connection, cache_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM metadata_cache WHERE cache_key=?", (cache_key,)
    ).fetchone()


def set_cached_metadata(conn: sqlite3.Connection, cache_key: str, data: dict) -> None:
    conn.execute(
        """INSERT INTO metadata_cache (cache_key, title_guess, year_guess, media_type,
               tmdb_id, tmdb_title, tmdb_year, queried_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(cache_key) DO UPDATE SET
               title_guess=excluded.title_guess, year_guess=excluded.year_guess,
               media_type=excluded.media_type, tmdb_id=excluded.tmdb_id,
               tmdb_title=excluded.tmdb_title, tmdb_year=excluded.tmdb_year,
               queried_at=excluded.queried_at""",
        (
            cache_key,
            data.get("title_guess"),
            data.get("year_guess"),
            data.get("media_type"),
            data.get("tmdb_id"),
            data.get("tmdb_title"),
            data.get("tmdb_year"),
            time.time(),
        ),
    )


def update_file_guess(conn: sqlite3.Connection, file_id: int, title: str | None, year: int | None, media_type: str | None) -> None:
    conn.execute(
        "UPDATE files SET title_guess=?, year_guess=?, media_type_guess=? WHERE id=?",
        (title, year, media_type, file_id),
    )


def delete_file_row(conn: sqlite3.Connection, root_label: str, rel_path: str) -> None:
    conn.execute("DELETE FROM files WHERE root_label=? AND rel_path=?", (root_label, rel_path))


def add_quarantine_record(
    conn: sqlite3.Connection, original_root: str, original_rel_path: str, quarantined_path: str, size: int | None, reason: str
) -> int:
    cur = conn.execute(
        """INSERT INTO quarantine (original_root, original_rel_path, quarantined_path, size, reason, quarantined_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (original_root, original_rel_path, quarantined_path, size, reason, time.time()),
    )
    return cur.lastrowid


def list_quarantine(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM quarantine ORDER BY quarantined_at DESC").fetchall()


def get_quarantine(conn: sqlite3.Connection, qid: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM quarantine WHERE id=?", (qid,)).fetchone()


def remove_quarantine_record(conn: sqlite3.Connection, qid: int) -> None:
    conn.execute("DELETE FROM quarantine WHERE id=?", (qid,))
