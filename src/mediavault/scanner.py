"""Walks a configured root, updating the SQLite index.

Cloud-aware: files not yet downloaded (iCloud placeholders) are recorded
without hashing (hashing would force a download) and are never treated as
missing/deleted just because their content isn't local yet.
"""
from __future__ import annotations

import errno
import sqlite3
import time
from pathlib import Path

from . import cloud, db
from .config import RootConfig
from .hashing import hash_file, quick_signature, resolve_algo


# A single file that can't be read right now isn't a reason to abandon the
# whole root: EDEADLK is macOS refusing to download an iCloud file for a
# background process; EACCES/EPERM is a privacy (TCC) or permission refusal.
# Everything else (EIO, ENOTCONN, ESTALE, ...) means the drive or share itself
# is in trouble and still aborts the scan.
_SKIPPABLE_FILE_ERRNOS = {errno.EDEADLK, errno.EACCES, errno.EPERM}


def _keep_as_unreadable(conn, root, rel_path: str, size=None) -> None:
    """Record a file that is present but can't be read right now, the way a
    not-downloaded placeholder is: indexed (so it isn't mistaken for deleted),
    with whatever size/hash we already knew, and no new hashing."""
    existing = db.get_file(conn, root.label, rel_path)
    db.upsert_file(
        conn, root.label, rel_path,
        size=existing["size"] if existing else size,
        mtime=existing["mtime"] if existing else None,
        hash_=existing["hash"] if existing else None,
        is_placeholder=True,
    )


def scan_root(conn: sqlite3.Connection, root: RootConfig, hash_algo: str = "blake3") -> dict:
    db.upsert_root(conn, root.label, str(root.path), root.role, root.mode)

    if not root.path.exists():
        raise FileNotFoundError(f"Root {root.label!r} path does not exist: {root.path}")

    # Rows for OS junk indexed before junk.py existed: delete them outright up
    # front so they never show up as sync conflicts/promotions/"gone" files.
    db.purge_junk_rows(conn, root.label)

    # The algorithm that will actually be used (blake3 falls back to sha256
    # when the package isn't installed). Hashes carry their algorithm as a
    # prefix, and only same-algorithm hashes can be compared, so a cached hash
    # from a different algorithm must not be reused -- see below.
    algo = resolve_algo(hash_algo)
    scan_started_at = time.time()
    scan_id = db.start_scan(conn, root.label)

    try:
        stats = {"files_scanned": 0, "new_files": 0, "updated_files": 0, "missing_files": 0, "placeholders": 0, "unreadable": 0}
        last_progress_commit = time.monotonic()

        for entry in cloud.walk_cloud_aware(root.path):
            stats["files_scanned"] += 1

            # A large library can take a long time to hash. Commit progress
            # periodically (throttled to roughly once a second, not every file --
            # fine whether files are few-and-huge or many-and-tiny) so another
            # connection (the web UI) can show it's actually moving, and so an
            # interrupted scan (crash, sleep, force-quit) only has to re-hash
            # whatever wasn't committed yet, not start over from scratch.
            now = time.monotonic()
            if now - last_progress_commit >= 1.0:
                db.update_scan_progress(conn, scan_id, stats["files_scanned"])
                conn.commit()
                last_progress_commit = now

            if not entry.downloaded:
                stats["placeholders"] += 1
                existing = db.get_file(conn, root.label, entry.rel_path)
                if existing is None:
                    stats["new_files"] += 1
                db.upsert_file(
                    conn, root.label, entry.rel_path,
                    size=existing["size"] if existing else entry.size,
                    mtime=existing["mtime"] if existing else None,
                    hash_=existing["hash"] if existing else None,
                    is_placeholder=True,
                )
                continue

            full_path = root.path / entry.rel_path
            try:
                size, mtime = quick_signature(full_path)
            except OSError as exc:
                if exc.errno not in _SKIPPABLE_FILE_ERRNOS:
                    raise
                stats["unreadable"] += 1
                _keep_as_unreadable(conn, root, entry.rel_path)
                continue
            existing = db.get_file(conn, root.label, entry.rel_path)

            if (
                existing is not None and existing["size"] == size and existing["mtime"] == mtime
                and existing["hash"] and existing["hash"].startswith(f"{algo}:")
            ):
                # Unchanged since last scan — reuse the cached hash, skip re-reading the file.
                # (Only if it was made with the algorithm in use now: otherwise the same
                # content would carry different hashes on different files, and duplicate
                # and sync matching would silently miss them. Changing hash_algo, or
                # installing blake3 while set to it, re-hashes everything once instead.)
                db.upsert_file(conn, root.label, entry.rel_path, size, mtime, existing["hash"], is_placeholder=False)
                continue

            try:
                file_hash = hash_file(full_path, algo=algo)
            except OSError as exc:
                if exc.errno not in _SKIPPABLE_FILE_ERRNOS:
                    raise
                stats["unreadable"] += 1
                _keep_as_unreadable(conn, root, entry.rel_path, size=size)
                continue
            if existing is None:
                stats["new_files"] += 1
            else:
                stats["updated_files"] += 1
            db.upsert_file(conn, root.label, entry.rel_path, size, mtime, file_hash, is_placeholder=False)

        stats["missing_files"] = db.mark_missing(conn, root.label, scan_started_at)
    except BaseException as exc:
        # An unreadable file, a network share dropping mid-walk, Ctrl-C...
        # Without this the scan row stayed "running" (finished_at NULL) for
        # up to 6 h, and the UI claimed it was still scanning. Keep what was
        # indexed (each upsert is complete on its own); mark_missing never ran,
        # so nothing is wrongly treated as deleted.
        conn.commit()
        db.fail_scan(conn, scan_id, f"{type(exc).__name__}: {exc}")
        conn.commit()
        raise
    db.finish_scan(conn, scan_id, stats)
    return stats
