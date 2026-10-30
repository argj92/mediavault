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
    the sync feature working as intended, not something to clean up: primary,
    every mirror-mode backup that's supposed to hold a full copy of it, and any
    iCloud root -- what lives in iCloud is a deliberate choice (it holds only
    the part of the library you want available there), so a copy there is
    never "the extra one". watch roots (Downloads, inboxes) are never included:
    a file also turning up there is still worth flagging."""
    primary_row = db.get_primary_root(conn)
    if primary_row is None:
        return set()
    labels = {primary_row["label"]}
    for row in db.syncable_roots(conn, exclude_label=primary_row["label"]):
        if row["mode"] == "mirror":
            labels.add(row["label"])
    for row in db.list_roots(conn, enabled_only=True):
        if row["role"] == "icloud":
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


def _best_copy(files, path_counts: dict[str, int] | None = None):
    """The copy worth keeping among equals. First choice: a path that matches
    where the same content sits in other places (a mirror is only tidy when
    both drives use the same path -- keep the copy that already lines up and
    the odd one out is the extra). Then the shortest path (the tidiest
    location -- "Movies/X.mkv" over "Movies/Other Movie/X copy.mkv"), then
    alphabetical so the choice is deterministic."""
    counts = path_counts or {}
    return min(files, key=lambda f: (-counts.get(f["rel_path"], 0), len(f["rel_path"]), f["rel_path"], f["root_label"]))


def split_keepers_extras(files, expected_labels: set[str]) -> tuple[list, list]:
    """Which copies of a duplicated file to keep vs. remove. Keep one copy in
    each primary/mirror-backup root that has one (that's the designed
    redundancy -- see _expected_mirror_labels); everything else is an extra:
    a second copy within the same root, or a copy outside the designed backup
    set. If the content isn't in any designed root at all, keep one copy
    overall. Within a root, the copy whose path matches the same file on
    another drive wins (see _best_copy)."""
    path_counts: dict[str, int] = {}
    for f in files:
        path_counts[f["rel_path"]] = path_counts.get(f["rel_path"], 0) + 1
    by_root: dict[str, list] = {}
    for f in files:
        by_root.setdefault(f["root_label"], []).append(f)
    designed = [label for label in by_root if label in expected_labels]
    keepers = [_best_copy(by_root[label], path_counts) for label in designed] or [_best_copy(files, path_counts)]
    keep_ids = {k["id"] for k in keepers}
    return keepers, [f for f in files if f["id"] not in keep_ids]


# Size buckets for the Duplicates histogram: log scale, a factor of 4 apart,
# with rough human labels (nobody wants "4.137 KB"). Edges are in bytes.
_KB = 1024
SIZE_BUCKETS = [
    ("< 1 KB", 0, _KB),
    ("1–4 KB", _KB, 4 * _KB),
    ("4–16 KB", 4 * _KB, 16 * _KB),
    ("16–64 KB", 16 * _KB, 64 * _KB),
    ("64–256 KB", 64 * _KB, 256 * _KB),
    ("256 KB–1 MB", 256 * _KB, _KB**2),
    ("1–4 MB", _KB**2, 4 * _KB**2),
    ("4–16 MB", 4 * _KB**2, 16 * _KB**2),
    ("16–64 MB", 16 * _KB**2, 64 * _KB**2),
    ("64–256 MB", 64 * _KB**2, 256 * _KB**2),
    ("256 MB–1 GB", 256 * _KB**2, _KB**3),
    ("1–4 GB", _KB**3, 4 * _KB**3),
    ("4 GB+", 4 * _KB**3, None),
]


def size_bucket_index(size: int | None) -> int:
    size = size or 0
    for i, (_, lo, hi) in enumerate(SIZE_BUCKETS):
        if hi is None or size < hi:
            return i
    return len(SIZE_BUCKETS) - 1


def find_duplicates(conn: sqlite3.Connection) -> list[dict]:
    expected_labels = _expected_mirror_labels(conn)
    groups = []
    for g in db.duplicate_groups(conn):
        if _is_expected_backup_redundancy(g["files"], expected_labels):
            continue  # e.g. primary + its full mirror backup both correctly holding this file
        keepers, extras = split_keepers_extras(g["files"], expected_labels)
        each = g["total_size"] // g["count"] if g["count"] else 0
        g["each_size"] = each
        g["keepers"] = keepers
        g["extras"] = extras
        # What "keep one per designed root, remove the rest" frees -- not
        # (count - 1) copies, since a copy on the backup root is meant to be there.
        g["wasted_bytes"] = len(extras) * each
        g["bucket"] = size_bucket_index(each)
        groups.append(g)
    return groups


def total_wasted_bytes(groups: list[dict]) -> int:
    return sum(g["wasted_bytes"] for g in groups)


def size_histogram(groups: list[dict]) -> list[dict]:
    """One entry per size bucket (empty ones included, so the axis stays the
    same and bars don't jump around as groups are cleaned up): how many
    duplicate groups fall there, how many extra copies, and what removing
    them frees. `pct` scales the bar against the fullest bucket."""
    buckets = [
        {"index": i, "label": label, "lo": lo, "hi": hi, "groups": 0, "extras": 0, "wasted_bytes": 0}
        for i, (label, lo, hi) in enumerate(SIZE_BUCKETS)
    ]
    for g in groups:
        b = buckets[g["bucket"]]
        b["groups"] += 1
        b["extras"] += len(g["extras"])
        b["wasted_bytes"] += g["wasted_bytes"]
    peak = max((b["groups"] for b in buckets), default=0)
    for b in buckets:
        # A visible sliver for any non-empty bucket, even next to a huge one.
        b["pct"] = 0 if not b["groups"] else max(3, round(100 * b["groups"] / peak))
    return buckets


def quarantine_groups(
    conn: sqlite3.Connection, quarantine_dir: Path, hashes: list[str], mode: str
) -> dict:
    """Bulk quarantine for the Duplicates section. `mode`:
      - "extras": per group, keep one copy per designed root and quarantine the rest
      - "all":    quarantine every copy (removes the content from every drive --
                  still recoverable from the quarantine until it's purged)

    Only groups the Duplicates section is actually showing are eligible:
    `hashes` comes from the browser, but each is looked up in a fresh
    find_duplicates() -- so a stale page, or a hand-crafted request naming an
    expected primary/backup pair, can't quarantine anything the UI wouldn't
    have offered. Each success is committed immediately: files have already
    moved on disk, and a failure partway through a long run must not roll the
    index back out from under them."""
    if mode not in ("extras", "all"):
        raise ValueError(f"unknown mode {mode!r}")
    shown = {g["hash"]: g for g in find_duplicates(conn)}
    result = {"groups": 0, "quarantined": 0, "bytes": 0, "skipped": [], "ignored": 0}
    for h in dict.fromkeys(hashes):  # de-duplicated, order kept
        g = shown.get(h)
        if g is None:
            result["ignored"] += 1
            continue
        victims = g["extras"] if mode == "extras" else list(g["files"])
        moved_any = False
        for f in victims:
            root_row = db.get_root(conn, f["root_label"])
            if root_row is None:
                result["skipped"].append(f"{f['root_label']}: root no longer configured")
                continue
            try:
                quarantine_duplicate(conn, quarantine_dir, Path(root_row["path"]), f["root_label"], f["rel_path"])
            except QuarantineError as exc:
                result["skipped"].append(f"{f['root_label']}/{f['rel_path']}: {exc}")
            else:
                result["quarantined"] += 1
                result["bytes"] += f["size"] or 0
                moved_any = True
            conn.commit()  # also persists the stale-row cleanup a QuarantineError can carry
        result["groups"] += moved_any
    return result


def purge_quarantine(conn: sqlite3.Connection, older_than_days: float | None = None) -> dict:
    """Permanently deletes quarantined files (all of them, or only those
    quarantined more than `older_than_days` ago) and drops their records.
    This is the one irreversible step in duplicate cleanup. A file already
    gone from the quarantine folder just has its record cleared. Each delete
    is committed immediately: the file is gone, so the record must not
    survive a failure later in the run. Returns {purged, bytes, errors}."""
    cutoff = None if older_than_days is None else time.time() - older_than_days * 86400
    result = {"purged": 0, "bytes": 0, "errors": []}
    for q in db.list_quarantine(conn):
        if cutoff is not None and q["quarantined_at"] > cutoff:
            continue
        try:
            Path(q["quarantined_path"]).unlink(missing_ok=True)
        except OSError as exc:
            result["errors"].append(f"{q['original_root']}/{q['original_rel_path']}: {exc.strerror or exc}")
            continue
        db.remove_quarantine_record(conn, q["id"])
        conn.commit()
        result["purged"] += 1
        result["bytes"] += q["size"] or 0
    return result


class QuarantineError(Exception):
    """A quarantine that couldn't (fully) happen for a reason worth telling
    the user in plain words -- an unmounted drive, a file that's already
    gone, a copy that died partway -- as opposed to a bug."""


def quarantine_duplicate(conn: sqlite3.Connection, quarantine_dir: Path, root_path: Path, root_label: str, rel_path: str) -> Path:
    """Moves one duplicate copy out of its root into quarantine and drops it
    from the index (it's intentionally gone from that root now).

    Raises QuarantineError, with nothing changed on disk or in the index,
    unless noted: a file that's already gone from a *reachable* root has its
    stale index row dropped (so the dashboard stops listing it) before the
    error is raised -- callers must let that one commit."""
    if not root_path.exists():
        raise QuarantineError(
            f"'{root_label}' isn't reachable right now (drive or network share not mounted?). "
            "Reconnect it and try again -- nothing was changed."
        )

    src = root_path / rel_path
    if not src.exists():
        db.delete_file_row(conn, root_label, rel_path)
        raise QuarantineError(
            f"'{rel_path}' is already gone from {root_label}; removed the stale entry from the index."
        )
    size = src.stat().st_size

    dest = quarantine_dir / root_label / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest = dest.with_name(f"{dest.stem}.{int(time.time())}{dest.suffix}")

    try:
        shutil.move(str(src), str(dest))
    except OSError as exc:
        # Across filesystems (the usual case: network share -> local disk) a
        # move is copy-then-delete. If the copy dies partway -- share drops,
        # disk full -- a truncated file is left in quarantine while the
        # original is untouched. Don't leave the truncated one behind to be
        # mistaken for a real, restorable copy.
        if src.exists():
            dest.unlink(missing_ok=True)
        raise QuarantineError(
            f"Couldn't move '{rel_path}' out of {root_label}: {exc.strerror or exc}. Nothing was changed."
        ) from exc

    db.add_quarantine_record(conn, root_label, rel_path, str(dest), size, reason="duplicate")
    db.delete_file_row(conn, root_label, rel_path)
    return dest
