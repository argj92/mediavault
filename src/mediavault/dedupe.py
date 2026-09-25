"""Finds duplicate files (identical content, by hash) across every tracked
root, and lets you safely remove extra copies.

Removing a duplicate never deletes it outright — it's moved into mediavault's
own quarantine folder (see recycle_bin.py), so it can still be recovered
until you deliberately empty that quarantine.
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

from . import db


def find_duplicates(conn: sqlite3.Connection) -> list[dict]:
    groups = db.duplicate_groups(conn)
    for g in groups:
        g["wasted_bytes"] = (g["count"] - 1) * (g["total_size"] // g["count"] if g["count"] else 0)
    return groups


def total_wasted_bytes(groups: list[dict]) -> int:
    return sum(g["wasted_bytes"] for g in groups)


def quarantine_duplicate(conn: sqlite3.Connection, quarantine_dir: Path, root_path: Path, root_label: str, rel_path: str) -> Path:
    """Moves one duplicate copy out of its root into quarantine and drops it
    from the index (it's intentionally gone from that root now)."""
    src = root_path / rel_path
    size = src.stat().st_size if src.exists() else None

    dest = quarantine_dir / root_label / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest = dest.with_name(f"{dest.stem}.{int(time.time())}{dest.suffix}")

    shutil.move(str(src), str(dest))
    db.add_quarantine_record(conn, root_label, rel_path, str(dest), size, reason="duplicate")
    db.delete_file_row(conn, root_label, rel_path)
    return dest
