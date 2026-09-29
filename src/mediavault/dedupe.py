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


def _expected_mirror_labels(conn: sqlite3.Connection) -> set[str]:
    """Root labels where a file being fully synced (present, identical) is
    the sync feature working as intended, not something to clean up: primary
    plus every mirror-mode backup that's supposed to hold a full copy of it.
    subset/watch roots are never included -- a file also turning up there is
    still worth flagging (it's not part of the designed backup set)."""
    primary_row = db.get_primary_root(conn)
    if primary_row is None:
        return set()
    labels = {primary_row["label"]}
    for row in db.syncable_roots(conn, exclude_label=primary_row["label"]):
        if row["mode"] == "mirror":
            labels.add(row["label"])
    return labels


def _is_expected_backup_redundancy(files, expected_labels: set[str]) -> bool:
    """True when every copy in the group sits in its own distinct
    primary/mirror-backup root -- i.e. this is exactly what a working
    full-mirror backup looks like (the same file correctly present on both
    primary and its backup), not an accidental extra copy. A file that's
    doubled up within one root, or that also turns up somewhere outside the
    designed backup set (Downloads, a subset/watch root), still counts as a
    real duplicate worth flagging."""
    root_labels = [f["root_label"] for f in files]
    if len(root_labels) < 2 or len(root_labels) != len(set(root_labels)):
        return False
    return set(root_labels) <= expected_labels


def find_duplicates(conn: sqlite3.Connection) -> list[dict]:
    expected_labels = _expected_mirror_labels(conn)
    groups = []
    for g in db.duplicate_groups(conn):
        if _is_expected_backup_redundancy(g["files"], expected_labels):
            continue  # e.g. primary + its full mirror backup both correctly holding this file
        g["wasted_bytes"] = (g["count"] - 1) * (g["total_size"] // g["count"] if g["count"] else 0)
        groups.append(g)
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
