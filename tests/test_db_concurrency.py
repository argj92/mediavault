"""Regression coverage for a real 'database is locked' crash: the background
worker (its own connection, on a timer) and a web request handling a manual
"Scan now" click (a connection per request) can legitimately write at the
same time. Default rollback-journal mode plus Python sqlite3's default 5s
busy timeout means a writer holding the lock for longer than that (easy for
a scan walking/hashing many files in one long transaction) makes any other
concurrent writer fail outright instead of just waiting its turn.

Reproduced directly (outside this suite, not run here since it needs a
deliberate >5s hold to cross Python's old default timeout): two plain
sqlite3 connections, one holding a write transaction for 6s, the second
started 0.5s later -- without WAL+busy_timeout the second fails with
"database is locked"; with them, it simply waits and succeeds. The fix is
two PRAGMAs in db.connect(), asserted directly below rather than re-raced
here to keep this test fast and non-flaky.

A second, narrower race was also reproduced directly against a brand-new
database file: `PRAGMA journal_mode = WAL` briefly needs exclusive-ish access
to switch a fresh file's journal mode, and running it unconditionally on
every single connect() meant two mediavault instances launched together
against a not-yet-existing db could both hit it at once -- observed as
"database is locked" raised from that statement itself, at app startup,
before any scan even started. db._ensure_wal_mode now checks the mode first
(a no-op read after the first connection ever) and retries a few times on
contention instead of propagating immediately.
"""
import sqlite3
import threading
import time

from mediavault import db


def test_connect_enables_wal_and_a_generous_busy_timeout(tmp_path):
    db_path = tmp_path / "mediavault.db"
    with db.connect(db_path) as conn:
        db.init_db(conn)
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]

    assert journal_mode.lower() == "wal"
    # Python's sqlite3 default is 5000ms -- comfortably less than a scan can
    # take. Require enough headroom that a real scan's transaction won't
    # outlast it and start throwing "database is locked" at other writers.
    assert busy_timeout_ms >= 20_000


class _ExecuteSpy:
    """sqlite3.Connection.execute is a read-only C attribute, so it can't be
    monkeypatched directly -- wrap the real connection instead. _ensure_wal_mode
    only ever calls .execute(sql), never anything else on the connection."""

    def __init__(self, real_conn, on_execute):
        self._real = real_conn
        self._on_execute = on_execute

    def execute(self, sql, *args):
        return self._on_execute(self._real, sql, *args)


def test_ensure_wal_mode_skips_the_write_once_already_wal(tmp_path):
    """After the first connection, journal_mode is already 'wal' on disk --
    every later connection must not re-issue the SET (that's the statement
    that raced at startup), only the cheap read-check."""
    db_path = tmp_path / "mediavault.db"
    with db.connect(db_path):
        pass  # first-ever connection: actually switches the mode

    real_conn = sqlite3.connect(str(db_path))
    executed = []

    def record(real, sql, *args):
        executed.append(sql)
        return real.execute(sql, *args)

    db._ensure_wal_mode(_ExecuteSpy(real_conn, record))
    real_conn.close()

    assert "PRAGMA journal_mode = WAL" not in executed


def test_ensure_wal_mode_retries_through_transient_contention(tmp_path, monkeypatch):
    """The exact failure observed: switching a fresh file's journal mode can
    raise 'database is locked' from another connection doing the same thing
    at the same instant. A transient one should be retried, not propagated."""
    db_path = tmp_path / "mediavault.db"
    real_conn = sqlite3.connect(str(db_path))
    attempts = {"n": 0}

    def flaky(real, sql, *args):
        if sql == "PRAGMA journal_mode = WAL":
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise sqlite3.OperationalError("database is locked")
        return real.execute(sql, *args)

    monkeypatch.setattr(time, "sleep", lambda _: None)  # don't actually wait in the test

    db._ensure_wal_mode(_ExecuteSpy(real_conn, flaky))  # must not raise

    assert attempts["n"] == 3
    real_conn.close()


def test_upsert_file_survives_two_connections_racing_on_the_same_new_file(tmp_path):
    """Reproduced from a real crash: two mediavault instances (`web` and
    `gui`) scanning the same root concurrently can both see 'no existing
    row' for the same brand-new file and both try to INSERT it, and the
    loser used to hit sqlite3.IntegrityError: UNIQUE constraint failed on
    (root_label, rel_path). upsert_file is now a single atomic
    INSERT ... ON CONFLICT DO UPDATE, so this must no longer be possible."""
    db_path = tmp_path / "mediavault.db"
    with db.connect(db_path) as conn:
        db.init_db(conn)

    barrier = threading.Barrier(2)
    errors = []

    def race():
        try:
            with db.connect(db_path) as conn:
                barrier.wait(timeout=5)  # line both threads up at the same instant
                db.upsert_file(conn, "root", "Movie.mkv", 100, 1.0, "sha256:abc", False)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=race)
    t2 = threading.Thread(target=race)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"concurrent upsert_file raised: {errors}"
    with db.connect(db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) AS n FROM files WHERE root_label='root' AND rel_path='Movie.mkv'").fetchone()
    assert rows["n"] == 1  # exactly one row, not a duplicate and not a crash
