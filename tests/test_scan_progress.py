"""A real library can take a long time to scan/hash. Without periodic
progress commits, a concurrent reader (the web UI) sees nothing update in
the scans table until the whole thing finishes -- for a multi-hour scan,
that's indistinguishable from "stuck" or "broken". scan_root now commits
progress periodically (throttled to ~1s of wall time) so db.get_active_scan
reflects it while running, not just once at the very end.
"""
from mediavault import db, scanner
from .test_scanner import make_root
from .conftest import write_file


def test_get_active_scan_and_last_finished_scan(conn, tmp_path):
    root = make_root(tmp_path, label="root")
    write_file(root.path, "Movie.mkv", b"content")

    assert db.get_active_scan(conn, "root") is None
    assert db.get_last_finished_scan(conn, "root") is None

    scanner.scan_root(conn, root, hash_algo="sha256")

    assert db.get_active_scan(conn, "root") is None  # finished, so no longer active
    last = db.get_last_finished_scan(conn, "root")
    assert last is not None
    assert last["files_scanned"] == 1


def test_get_active_scan_ignores_a_stale_abandoned_row(conn, tmp_path):
    """A crashed/force-quit process leaves its scan row stuck at
    finished_at IS NULL forever. Past a generous staleness window, that
    should read as 'not actually running' rather than falsely claim an
    in-progress scan that will never finish."""
    scan_id = db.start_scan(conn, "root")
    import time

    conn.execute("UPDATE scans SET started_at=? WHERE id=?", (time.time() - 7 * 3600, scan_id))

    assert db.get_active_scan(conn, "root", stale_after_seconds=6 * 3600) is None
    # A legitimately long-running scan (under the threshold) still counts as active.
    assert db.get_active_scan(conn, "root", stale_after_seconds=8 * 3600) is not None


def test_scan_root_updates_progress_more_than_once_for_a_multi_file_scan(conn, tmp_path, monkeypatch):
    root = make_root(tmp_path, label="root")
    for i in range(5):
        write_file(root.path, f"Movie{i}.mkv", f"content {i}".encode())

    from mediavault import scanner as scanner_mod

    progress_calls = []
    original_update = db.update_scan_progress

    def spy(c, scan_id, files_scanned):
        progress_calls.append(files_scanned)
        return original_update(c, scan_id, files_scanned)

    monkeypatch.setattr(db, "update_scan_progress", spy)

    # Force the ~1s throttle to fire on every file instead of waiting real
    # wall-clock time, so this test runs instantly.
    ticker = iter(range(0, 100))
    monkeypatch.setattr(scanner_mod.time, "monotonic", lambda: next(ticker))

    scanner.scan_root(conn, root, hash_algo="sha256")

    assert len(progress_calls) > 1
    assert progress_calls == sorted(progress_calls)  # strictly non-decreasing progress
