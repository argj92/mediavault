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

from . import dedupe, db, notify, schedule, suggestions, sync
from .config import AppConfig, RootConfig

log = logging.getLogger("mediavault.scheduler")

RETRY_AFTER_FAILURE_SECONDS = 60
RETRY_AFTER_SKIPPED_ROOT_SECONDS = 300


def _reachable(path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


# At most one scan per root at a time, however it was started (the schedule,
# "Scan all now", a root's own "Scan now"): scanning the same folder twice at
# once only doubles the reading and hashing.
_root_locks: dict[str, threading.Lock] = {}
_root_locks_guard = threading.Lock()

# Different roots are usually different drives (a USB disk, a network share,
# the internal disk), so they scan side by side. Capped so a library with many
# roots doesn't open that many readers and database writers at once.
MAX_PARALLEL_SCANS = 4


def root_lock(label: str) -> threading.Lock:
    with _root_locks_guard:
        return _root_locks.setdefault(label, threading.Lock())


def _scan_one(config: AppConfig, row) -> str | None:
    """Scan a single root on its own connection (sqlite3 connections belong to
    one thread). Returns the error text if it had to be skipped, else None. A
    root already being scanned elsewhere is left to that scan."""
    root = RootConfig.from_row(row)
    lock = root_lock(root.label)
    if not lock.acquire(blocking=False):
        log.info("%s is already being scanned; not starting a second scan", root.label)
        return None
    try:
        with db.connect(config.db_path) as conn:
            try:
                from . import reconnect, scanner

                if not _reachable(root.path):
                    # A network share that dropped off: if the user saved where to
                    # re-mount it from, try (throttled) before giving up on this cycle.
                    reconnect.try_auto(conn, row)
                scanner.scan_root(conn, root, hash_algo=config.hash_algo)
            except OSError as exc:
                # A removable/backup/iCloud/network drive not being mounted right
                # now (FileNotFoundError) is normal. A network mount that drops
                # mid-scan (stale SMB/NFS handle, sleep/wake, Wi-Fi hiccup) raises
                # some other OSError partway through the walk/stat/hash calls --
                # equally not worth losing the whole reconcile cycle over. Either
                # way, skip just this root and keep scanning the rest.
                log.warning("skipping scan for %s: %s", root.label, exc)
                return str(exc)
        return None
    finally:
        lock.release()


def scan_roots(conn: sqlite3.Connection, config: AppConfig, only: list[str] | None = None) -> list[str]:
    """Scans every enabled root (or just the labels in `only`), in parallel
    across roots. Returns the error text of each root that had to be skipped."""
    # The scans below write through their own connections. End any transaction
    # this one has open first: its writes must be visible to them, and it must
    # not hold the database's single write lock while they run.
    conn.commit()
    rows = [r for r in db.list_roots(conn, enabled_only=True) if only is None or r["label"] in only]
    if not rows:
        return []
    if len(rows) == 1:
        err = _scan_one(config, rows[0])
        return [err] if err else []
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(len(rows), MAX_PARALLEL_SCANS), thread_name_prefix="mediavault-scan") as pool:
        results = list(pool.map(lambda r: _scan_one(config, r), rows))
    return [e for e in results if e]


def reconcile_once(conn: sqlite3.Connection, config: AppConfig) -> dict:
    """Rescans every enabled root, recomputes duplicate groups and each
    root's sync status against the primary, and looks for untracked inbox
    media. Returns a summary dict."""
    scan_errors = scan_roots(conn, config)

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
    """Runs scans in a daemon thread: a full reconcile at start-up and then on
    the interval the user chose (schedule.py), plus scans the UI asks for
    ("Scan all now", "Scan now" on one root). Only one scan runs at a time, so
    a manual request while one is underway is refused rather than doubling the
    load on the drives. One DB connection is opened per cycle (sqlite3
    connections aren't safe to share across threads long-term)."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._running = threading.Lock()          # held while a full cycle is underway
        self._pending = threading.Lock()          # guards the fields below
        self._pending_full = False
        self._manual: set[str] = set()            # roots with a "Scan now" queued or running
        self._reschedule = False
        self._next_run_at: float | None = None
        self._last_summary: dict | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ requests
    def is_busy(self) -> bool:
        return self._running.locked() or self._pending_full or bool(self._manual)

    def request_run(self, labels: list[str] | None = None) -> bool:
        """Ask for a scan now: every root (`labels=None`), or just those roots.
        True if something was started or queued; False if that work is already
        underway (a full scan running or queued; every named root already being
        scanned). Root scans don't wait for each other -- different drives scan
        side by side -- and don't wait for a full cycle either."""
        if labels is None:
            with self._pending:
                if self._running.locked() or self._pending_full:
                    return False
                self._pending_full = True
            self._wake.set()
            return True
        started = False
        for label in labels:
            with self._pending:
                if label in self._manual or root_lock(label).locked():
                    continue
                self._manual.add(label)
            threading.Thread(target=self._scan_label, args=(label,), daemon=True, name=f"mediavault-scan-{label}").start()
            started = True
        return started

    def _scan_label(self, label: str) -> None:
        try:
            with db.connect(self.config.db_path) as conn:
                db.init_db(conn)
                scan_roots(conn, self.config, only=[label])
        except Exception:  # noqa: BLE001
            log.exception("scan of %s failed", label)
        finally:
            with self._pending:
                self._manual.discard(label)

    def reschedule(self) -> None:
        """The interval changed: recompute when the next scan is due."""
        with self._pending:
            self._reschedule = True
        self._wake.set()

    # ---------------------------------------------------------------- work
    def _run_cycle(self) -> dict:
        with db.connect(self.config.db_path) as conn:
            db.init_db(conn)
            summary = reconcile_once(conn, self.config)
            notify_on_changes(conn, summary, self.config)
            return summary

    def _record(self, fn, *args) -> None:
        try:
            with db.connect(self.config.db_path) as conn:
                fn(conn, *args)
        except Exception:  # noqa: BLE001 -- bookkeeping must never take a scan down
            log.exception("could not record scan timing")

    def _run(self, labels: list[str] | None = None) -> dict | None:
        """One full cycle (scan every root, duplicates, sync status,
        notifications) -- what the dashboard's last/next times track."""
        summary = None
        with self._running:
            started = time.time()
            self._record(schedule.record_cycle_start, started)
            try:
                summary = self._run_cycle()
                self._record(schedule.record_cycle_end, started, True, len(summary.get("scan_errors", [])))
            except Exception:  # noqa: BLE001 — one bad cycle shouldn't kill the worker
                log.exception("mediavault background scan failed")
                self._record(schedule.record_cycle_end, started, False, 0)
        return summary

    def _interval_seconds(self) -> float:
        try:
            with db.connect(self.config.db_path) as conn:
                return schedule.get_interval_minutes(conn, self.config) * 60
        except Exception:  # noqa: BLE001
            return self.config.scan_interval_minutes * 60

    def _next_delay(self, summary: dict | None, interval: float | None = None) -> float:
        """Seconds until the next cycle. After a cycle that failed outright,
        or skipped a root (drive/share not mounted), look again soon rather
        than waiting out the full interval -- a Network Drive that comes back
        shouldn't sit unindexed for another hour (an interval of 24 h would be
        a full day). Never *longer* than the configured interval."""
        interval = self._interval_seconds() if interval is None else interval
        if summary is None:
            return min(interval, RETRY_AFTER_FAILURE_SECONDS)
        if summary.get("scan_errors"):
            return min(interval, RETRY_AFTER_SKIPPED_ROOT_SECONDS)
        return interval

    def _plan_next(self, summary: dict | None, base: float | None = None) -> None:
        interval = self._interval_seconds()
        delay = self._next_delay(summary, interval)
        base = time.time() if base is None else base
        self._next_run_at = base + delay
        self._last_summary = summary
        reason = "retry" if delay < interval else ""
        try:
            with db.connect(self.config.db_path) as conn:
                schedule.set_next_run(conn, self._next_run_at, reason)
        except Exception:  # noqa: BLE001
            log.exception("could not record the next scan time")

    def _loop(self) -> None:
        due = True  # first cycle immediately; run in the background, not at start-up
        while not self._stop.is_set():
            with self._pending:
                full, self._pending_full = self._pending_full, False
            if full or due:
                summary = self._run()
                self._plan_next(summary)
                due = False
                continue
            with self._pending:
                resched, self._reschedule = self._reschedule, False
            if resched and self._next_run_at is not None:
                # keep the same starting point (the last finished cycle), just apply the new interval
                try:
                    with db.connect(self.config.db_path) as conn:
                        last = schedule.read_state(conn)["last_finished_at"]
                except Exception:  # noqa: BLE001
                    last = None
                self._plan_next(self._last_summary, base=last)
            remaining = (self._next_run_at or 0) - time.time()
            if remaining <= 0:
                due = True
                continue
            self._wake.wait(min(remaining, 60))
            self._wake.clear()

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
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
