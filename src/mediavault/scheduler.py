"""Keeps the local index fresh without the user having to remember to run
anything: a full reconcile always runs once at process startup, and then a
background thread reruns it on an interval, notifying only when something
actually changed (new duplicates, a pair going out of sync, new untracked
media) rather than on every tick.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time

from . import dedupe, db, notify, suggestions, sync
from .config import AppConfig, RootConfig

log = logging.getLogger("mediavault.scheduler")


def reconcile_once(conn: sqlite3.Connection, config: AppConfig) -> dict:
    """Rescans every enabled root, recomputes duplicate groups and sync-pair
    status, and looks for untracked inbox media. Returns a summary dict."""
    scan_errors = []
    for row in db.list_roots(conn, enabled_only=True):
        root = RootConfig.from_row(row)
        try:
            from . import scanner

            scanner.scan_root(conn, root, hash_algo=config.hash_algo)
        except FileNotFoundError as exc:
            # A removable/backup/iCloud drive not being mounted right now is
            # normal, not an error worth breaking the whole reconcile over.
            scan_errors.append(str(exc))
            log.warning("skipping scan for %s: %s", root.label, exc)

    dup_groups = dedupe.find_duplicates(conn)

    pair_summaries = []
    for pair in db.list_sync_pairs(conn):
        if not pair["enabled"]:
            continue
        root_a_row = db.get_root(conn, pair["root_a"])
        root_b_row = db.get_root(conn, pair["root_b"])
        if root_a_row is None or root_b_row is None:
            continue
        plan = sync.plan_sync(conn, RootConfig.from_row(root_a_row), RootConfig.from_row(root_b_row))
        status = "out_of_sync" if plan.out_of_sync else "in_sync"
        db.update_pair_status(conn, pair["id"], status)
        pair_summaries.append({"id": pair["id"], "a": pair["root_a"], "b": pair["root_b"], **plan.summary()})

    untracked = suggestions.untracked_media(conn, config.video_extensions)
    new_untracked = [u for u in untracked if not u["already_in_library"]]

    return {
        "scan_errors": scan_errors,
        "duplicate_groups": len(dup_groups),
        "wasted_bytes": dedupe.total_wasted_bytes(dup_groups),
        "pairs": pair_summaries,
        "untracked_count": len(new_untracked),
    }


def notify_on_changes(conn: sqlite3.Connection, summary: dict) -> None:
    last_dupes = db.get_worker_state(conn, "last_notified_duplicate_groups", 0)
    if summary["duplicate_groups"] > last_dupes:
        notify.notify(
            "MediaVault — new duplicates found",
            f"{summary['duplicate_groups']} duplicate group(s), "
            f"{summary['wasted_bytes'] / 1e9:.1f} GB reclaimable.",
        )
    db.set_worker_state(conn, "last_notified_duplicate_groups", summary["duplicate_groups"])

    last_pair_status: dict = db.get_worker_state(conn, "last_pair_status", {})
    new_pair_status = {}
    for p in summary["pairs"]:
        key = str(p["id"])
        new_pair_status[key] = p["out_of_sync"]
        if p["out_of_sync"] and not last_pair_status.get(key, False):
            notify.notify(
                "MediaVault — sync pair out of sync",
                f"{p['a']} <-> {p['b']}: {p['copy_to_a'] + p['copy_to_b']} file(s) to copy, "
                f"{p['delete_from_a'] + p['delete_from_b']} deletion(s) need review.",
            )
    db.set_worker_state(conn, "last_pair_status", new_pair_status)

    last_untracked = db.get_worker_state(conn, "last_notified_untracked", 0)
    if summary["untracked_count"] > last_untracked:
        notify.notify(
            "MediaVault — new media found",
            f"{summary['untracked_count']} file(s) in your inbox folders aren't in the library yet.",
        )
    db.set_worker_state(conn, "last_notified_untracked", summary["untracked_count"])


class BackgroundWorker:
    """Runs reconcile_once on a timer in a daemon thread. One DB connection is
    opened per cycle (sqlite3 connections aren't safe to share across
    threads long-term)."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run_cycle(self) -> dict:
        with db.connect(self.config.db_path) as conn:
            db.init_db(conn)
            summary = reconcile_once(conn, self.config)
            notify_on_changes(conn, summary)
            return summary

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._run_cycle()
            except Exception:  # noqa: BLE001 — one bad cycle shouldn't kill the worker
                log.exception("mediavault background reconcile cycle failed")
            self._stop.wait(self.config.scan_interval_minutes * 60)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # Run once, synchronously, before returning — startup should reflect
        # a fresh scan, not stale data from last time the app ran.
        self._run_cycle()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="mediavault-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
