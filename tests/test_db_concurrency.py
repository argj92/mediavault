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
"""
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
