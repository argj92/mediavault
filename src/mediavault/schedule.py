"""When background scans run, how long they take, and whether a chosen
frequency makes sense -- the data behind the dashboard's schedule panel.

The interval a user picks in the UI is stored in the database (worker_state),
so it changes live without editing config.yaml or restarting; config.yaml's
`scan_interval_minutes` is only the default until they pick one.
"""
from __future__ import annotations

import math
import sqlite3
import statistics
import time

from . import db

# (minutes, label) offered in the UI.
PRESETS = [
    (15, "15 minutes"), (30, "30 minutes"), (60, "1 hour"), (120, "2 hours"),
    (180, "3 hours"), (360, "6 hours"), (720, "12 hours"), (1440, "24 hours"),
]
MIN_MINUTES = 5
MAX_MINUTES = 24 * 60

_INTERVAL_KEY = "schedule:interval_minutes"
_CYCLE_KEY = "schedule:cycle"   # {started_at, finished_at, duration, ok, scan_errors, running}
_NEXT_KEY = "schedule:next"     # {at, reason}


def label_for(minutes: int) -> str:
    for m, label in PRESETS:
        if m == minutes:
            return label
    if minutes % 60 == 0:
        h = minutes // 60
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{minutes} minutes"


def get_interval_minutes(conn: sqlite3.Connection, config) -> int:
    chosen = db.get_worker_state(conn, _INTERVAL_KEY)
    if isinstance(chosen, int) and MIN_MINUTES <= chosen <= MAX_MINUTES:
        return chosen
    return max(MIN_MINUTES, min(MAX_MINUTES, int(config.scan_interval_minutes)))


def set_interval_minutes(conn: sqlite3.Connection, minutes) -> int:
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        raise ValueError("Choose a number of minutes.") from None
    if not MIN_MINUTES <= minutes <= MAX_MINUTES:
        raise ValueError(f"Choose between {MIN_MINUTES} minutes and 24 hours.")
    db.set_worker_state(conn, _INTERVAL_KEY, minutes)
    return minutes


def record_cycle_start(conn: sqlite3.Connection, now: float | None = None) -> float:
    now = time.time() if now is None else now
    prev = db.get_worker_state(conn, _CYCLE_KEY, {}) or {}
    db.set_worker_state(conn, _CYCLE_KEY, {**prev, "running": True, "started_at": now})
    return now


def record_cycle_end(conn: sqlite3.Connection, started_at: float, ok: bool, scan_errors: int, now: float | None = None) -> None:
    now = time.time() if now is None else now
    db.set_worker_state(conn, _CYCLE_KEY, {
        "running": False, "started_at": started_at, "finished_at": now,
        "duration": max(0.0, now - started_at), "ok": ok, "scan_errors": scan_errors,
    })


def set_next_run(conn: sqlite3.Connection, at: float | None, reason: str = "") -> None:
    db.set_worker_state(conn, _NEXT_KEY, {"at": at, "reason": reason})


def read_state(conn: sqlite3.Connection) -> dict:
    cycle = db.get_worker_state(conn, _CYCLE_KEY, {}) or {}
    nxt = db.get_worker_state(conn, _NEXT_KEY, {}) or {}
    return {
        "running": bool(cycle.get("running")),
        "started_at": cycle.get("started_at"),
        "last_finished_at": cycle.get("finished_at"),
        "last_duration": cycle.get("duration"),
        "last_ok": cycle.get("ok"),
        "last_scan_errors": cycle.get("scan_errors", 0),
        "next_run_at": nxt.get("at"),
        "next_reason": nxt.get("reason", ""),
    }


def scan_estimates(conn: sqlite3.Connection) -> dict:
    """How long scans actually take here, from the scans table: per root the
    median of its recent successful scans (so the one-off first scan that
    hashed everything doesn't dominate), then the slowest root, plus how long
    the whole last cycle took. None where there's no data yet."""
    per_root: dict[str, float] = {}
    for r in db.list_roots(conn):
        rows = conn.execute(
            "SELECT finished_at - started_at AS d FROM scans WHERE root_label=? AND finished_at IS NOT NULL "
            "AND error IS NULL ORDER BY finished_at DESC LIMIT 5",
            (r["label"],),
        ).fetchall()
        durations = [row["d"] for row in rows if row["d"] is not None and row["d"] >= 0]
        if durations:
            per_root[r["label"]] = statistics.median(durations)
    largest_label = max(per_root, key=per_root.get) if per_root else None
    cycle = db.get_worker_state(conn, _CYCLE_KEY, {}) or {}
    cycle_seconds = cycle.get("duration") if cycle.get("ok") else None
    return {
        "per_root": per_root,
        "largest_label": largest_label,
        "largest_seconds": per_root[largest_label] if largest_label else None,
        "cycle_seconds": cycle_seconds,
    }


def human_duration(seconds: float) -> str:
    seconds = max(0, round(seconds))
    if seconds < 90:
        return f"{seconds} sec"
    minutes = seconds / 60
    if minutes < 90:
        return f"{round(minutes)} min"
    hours = minutes / 60
    return f"{hours:.1f} hours".replace(".0 ", " ")


def advise(interval_minutes: int, est: dict) -> dict:
    """Is this frequency sensible for how long scans take? level: ok | note |
    warn. A scan that takes longer than the interval just runs back-to-back
    (the next one can't start until the last finishes), keeping the drives
    permanently busy."""
    longest = max((s for s in (est.get("cycle_seconds"), est.get("largest_seconds")) if s), default=None)
    if longest is None:
        return {"level": "ok", "message": "", "suggested_minutes": None}
    interval_s = interval_minutes * 60
    needed_minutes = longest * 1.5 / 60
    suggested = next((m for m, _ in PRESETS if m >= needed_minutes and m >= MIN_MINUTES), PRESETS[-1][0])
    label, largest = est.get("largest_label"), est.get("largest_seconds")
    if label and largest and largest >= longest * 0.9:
        took = f"A full scan of your largest drive ({label}) takes about {human_duration(longest)}."
    elif label and largest:
        took = (f"A full pass over all your drives currently takes about {human_duration(longest)} "
                f"(the largest, {label}, takes about {human_duration(largest)} of that).")
    else:
        took = f"A full pass currently takes about {human_duration(longest)}."
    if interval_s < longest:
        return {
            "level": "warn",
            "message": f"{took} Every {label_for(interval_minutes)} is shorter than that, so scans would run back-to-back and keep your drives busy all the time. "
                       f"Try {label_for(suggested)} or longer.",
            "suggested_minutes": suggested,
        }
    if interval_s < longest * 2:
        return {
            "level": "note",
            "message": f"{took} Every {label_for(interval_minutes)} leaves little rest between scans; {label_for(suggested)} would be gentler.",
            "suggested_minutes": suggested,
        }
    return {"level": "ok", "message": f"{took} Every {label_for(interval_minutes)} leaves plenty of room.", "suggested_minutes": None}


def snapshot(conn: sqlite3.Connection, config) -> dict:
    """Everything the dashboard's schedule panel shows, in one JSON-able dict."""
    interval = get_interval_minutes(conn, config)
    est = scan_estimates(conn)
    return {
        **read_state(conn),
        "interval_minutes": interval,
        "interval_label": label_for(interval),
        "default_minutes": int(config.scan_interval_minutes),
        "estimates": {k: v for k, v in est.items() if k != "per_root"},
        "advice": advise(interval, est),
        "presets": [{"minutes": m, "label": l} for m, l in PRESETS],
    }
