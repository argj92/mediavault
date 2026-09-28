"""Keeps the local index fresh without the user having to remember to run
anything: a full reconcile runs immediately in the background when the app
starts, and then on an interval after that, notifying only when something
actually changed (new duplicates, a root drifting out of sync with the
primary, new untracked media) rather than on every tick. The first cycle
runs in the background rather than blocking startup -- for a real library a
full scan/hash can take a while, and nothing else (the GUI window opening,
the web server answering any request) should have to wait on it.
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
    """Rescans every enabled root, recomputes duplicate groups and each
    root's sync status against the primary, and looks for untracked inbox
    media. Returns a summary dict."""
    scan_errors = []
    for row in db.list_roots(conn, enabled_only=True):
        root = RootConfig.from_row(row)
        try:
            from . import scanner

            scanner.scan_root(conn, root, hash_algo=config.hash_algo)
        except OSError as exc:
            # A removable/backup/iCloud/network drive not being mounted right
            # now (FileNotFoundError) is normal. A network mount that drops
            # mid-scan (stale SMB/NFS handle, sleep/wake, Wi-Fi hiccup) raises
            # some other OSError partway through the walk/stat/hash calls --
            # equally not worth losing the whole reconcile cycle over. Either
            # way, skip just this root and keep scanning the rest.
            scan_errors.append(str(exc))
            log.warning("skipping scan for %s: %s", root.label, exc)

    dup_groups = dedupe.find_duplicates(conn)

    status_summaries = [
        {"label": s["root"].label, **s["plan"].summary()} for s in sync.sync_status_for_all(conn)
    ]

    untracked = suggestions.untracked_media(conn, config.video_extensions)
    new_untracked = [u for u in untracked if not u["already_in_library"]]

    return {
        "scan_errors": scan_errors,
        "duplicate_groups": len(dup_groups),
        "wasted_bytes": dedupe.total_wasted_bytes(dup_groups),
        "sync_status": status_summaries,
        "untracked_count": len(new_untracked),
    }


def notify_on_changes(conn: sqlite3.Connection, summary: dict, config: AppConfig) -> None:
    """The "last_notified_*"/"last_sync_status" baselines are always kept
    current, even with notifications_enabled=False -- so turning them back
    on later only notifies about genuinely new changes from that point on,
    not a backlog of everything that happened while they were off."""
    last_dupes = db.get_worker_state(conn, "last_notified_duplicate_groups", 0)
    if summary["duplicate_groups"] > last_dupes and config.notifications_enabled:
        notify.notify(
            "MediaVault — new duplicates found",
            f"{summary['duplicate_groups']} duplicate group(s), "
            f"{summary['wasted_bytes'] / 1e9:.1f} GB reclaimable.",
        )
    db.set_worker_state(conn, "last_notified_duplicate_groups", summary["duplicate_groups"])

    last_sync_status: dict = db.get_worker_state(conn, "last_sync_status", {})
    new_sync_status = {}
    for s in summary["sync_status"]:
        key = s["label"]
        new_sync_status[key] = s["out_of_sync"]
        if s["out_of_sync"] and not last_sync_status.get(key, False) and config.notifications_enabled:
            notify.notify(
                "MediaVault — out of sync with primary",
                f"{s['label']}: {s['copy_to_a'] + s['copy_to_b']} file(s) to copy, "
                f"{s['delete_from_a'] + s['delete_from_b']} deletion(s) need review.",
            )
    db.set_worker_state(conn, "last_sync_status", new_sync_status)

    last_untracked = db.get_worker_state(conn, "last_notified_untracked", 0)
    if summary["untracked_count"] > last_untracked and config.notifications_enabled:
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
            notify_on_changes(conn, summary, self.config)
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
        # _loop() runs a cycle immediately as its first action, so the first
        # scan still happens right away -- just in the background, not here.
        # Running it synchronously here used to block the whole app's startup
        # (the GUI window, or the web server accepting any request at all)
        # until every root was fully scanned/hashed, which for a real library
        # can take minutes; worse, holding that long a write transaction open
        # during startup is exactly what made a second concurrently-starting
        # instance crash with "database is locked" instead of just waiting.
        self._thread = threading.Thread(target=self._loop, daemon=True, name="mediavault-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
