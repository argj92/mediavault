"""Walks a configured root, updating the SQLite index.

Cloud-aware: files not yet downloaded (iCloud placeholders) are recorded
without hashing (hashing would force a download) and are never treated as
missing/deleted just because their content isn't local yet.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from . import cloud, db
from .config import RootConfig
from .hashing import hash_file, quick_signature


def scan_root(conn: sqlite3.Connection, root: RootConfig, hash_algo: str = "blake3") -> dict:
    db.upsert_root(conn, root.label, str(root.path), root.role, root.mode)

    if not root.path.exists():
        raise FileNotFoundError(f"Root {root.label!r} path does not exist: {root.path}")

    scan_started_at = time.time()
    scan_id = db.start_scan(conn, root.label)

    stats = {"files_scanned": 0, "new_files": 0, "updated_files": 0, "missing_files": 0, "placeholders": 0}

    for entry in cloud.walk_cloud_aware(root.path):
        stats["files_scanned"] += 1

        if not entry.downloaded:
            stats["placeholders"] += 1
            existing = db.get_file(conn, root.label, entry.rel_path)
            if existing is None:
                stats["new_files"] += 1
            db.upsert_file(
                conn, root.label, entry.rel_path,
                size=existing["size"] if existing else None,
                mtime=existing["mtime"] if existing else None,
                hash_=existing["hash"] if existing else None,
                is_placeholder=True,
            )
            continue

        full_path = root.path / entry.rel_path
        size, mtime = quick_signature(full_path)
        existing = db.get_file(conn, root.label, entry.rel_path)

        if existing is not None and existing["size"] == size and existing["mtime"] == mtime and existing["hash"]:
            # Unchanged since last scan — reuse the cached hash, skip re-reading the file.
            db.upsert_file(conn, root.label, entry.rel_path, size, mtime, existing["hash"], is_placeholder=False)
            continue

        file_hash = hash_file(full_path, algo=hash_algo)
        if existing is None:
            stats["new_files"] += 1
        else:
            stats["updated_files"] += 1
        db.upsert_file(conn, root.label, entry.rel_path, size, mtime, file_hash, is_placeholder=False)

    stats["missing_files"] = db.mark_missing(conn, root.label, scan_started_at)
    db.finish_scan(conn, scan_id, stats)
    return stats
