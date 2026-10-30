"""A scan that dies must not look like it's still running; scan history must
not grow forever; one instance per database; failed cycles are retried soon."""
import os
import subprocess
import sys
import textwrap

import pytest

from mediavault import db, instance, scanner, scheduler
from mediavault.config import AppConfig

from .conftest import write_file
from .test_scanner import make_root


def _boom_on(name):
    real = scanner.hash_file

    def hash_file(path, algo="sha256"):
        if path.name == name:
            raise OSError(5, "Input/output error")
        return real(path, algo=algo)

    return hash_file


def test_a_scan_that_dies_is_recorded_as_failed_not_left_running(conn, tmp_path, monkeypatch):
    root = make_root(tmp_path, label="lib", role="primary", mode="mirror")
    write_file(root.path, "A.mkv", b"aaa")
    write_file(root.path, "B.mkv", b"bbb")
    scanner.scan_root(conn, root, hash_algo="sha256")        # a good scan first
    write_file(root.path, "C.mkv", b"ccc")
    monkeypatch.setattr(scanner, "hash_file", _boom_on("C.mkv"))

    with pytest.raises(OSError):
        scanner.scan_root(conn, root, hash_algo="sha256")

    assert db.get_active_scan(conn, "lib") is None                       # no phantom "scanning..."
    failed = db.get_abandoned_scan(conn, "lib")
    assert failed is not None and "Input/output error" in failed["error"]
    # the earlier success is still "the last scan"; the failure isn't mistaken for one
    assert db.get_last_finished_scan(conn, "lib")["error"] is None
    # nothing was wrongly treated as deleted, and what was indexed stays indexed
    assert {f["rel_path"] for f in db.all_files(conn, root_label="lib")} >= {"A.mkv", "B.mkv"}
    assert conn.execute("SELECT COUNT(*) FROM files WHERE root_label='lib' AND missing=1").fetchone()[0] == 0


def test_failed_first_scan_is_not_reported_as_never_scanned(conn, tmp_path, monkeypatch):
    root = make_root(tmp_path, label="lib", role="primary", mode="mirror")
    write_file(root.path, "A.mkv", b"aaa")
    write_file(root.path, "B.mkv", b"bbb")
    monkeypatch.setattr(scanner, "hash_file", _boom_on("B.mkv"))

    with pytest.raises(OSError):
        scanner.scan_root(conn, root, hash_algo="sha256")

    assert db.get_last_finished_scan(conn, "lib") is None
    assert db.get_abandoned_scan(conn, "lib") is not None      # the UI says "N file(s) indexed, didn't finish"


def test_scan_history_is_pruned_but_keeps_the_last_success(conn, tmp_path, monkeypatch):
    root = make_root(tmp_path, label="lib", role="primary", mode="mirror")
    write_file(root.path, "A.mkv", b"aaa")
    scanner.scan_root(conn, root, hash_algo="sha256")
    good_id = db.get_last_finished_scan(conn, "lib")["id"]
    monkeypatch.setattr(scanner, "hash_file", _boom_on("A.mkv"))
    write_file(root.path, "A.mkv", b"changed so it must be re-hashed")
    for _ in range(db.SCAN_HISTORY_PER_ROOT + 10):
        with pytest.raises(OSError):
            scanner.scan_root(conn, root, hash_algo="sha256")

    n = conn.execute("SELECT COUNT(*) FROM scans WHERE root_label='lib'").fetchone()[0]
    assert n <= db.SCAN_HISTORY_PER_ROOT + 1
    assert db.get_last_finished_scan(conn, "lib")["id"] == good_id


def test_successful_scans_are_pruned_too(conn, tmp_path):
    root = make_root(tmp_path, label="lib", role="primary", mode="mirror")
    write_file(root.path, "A.mkv", b"aaa")
    for _ in range(db.SCAN_HISTORY_PER_ROOT + 15):
        scanner.scan_root(conn, root, hash_algo="sha256")
    assert conn.execute("SELECT COUNT(*) FROM scans WHERE root_label='lib'").fetchone()[0] == db.SCAN_HISTORY_PER_ROOT


def test_init_db_adds_the_error_column_to_an_existing_database(tmp_path):
    import sqlite3

    c = sqlite3.connect(tmp_path / "old.db")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE scans (id INTEGER PRIMARY KEY AUTOINCREMENT, root_label TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL, "
              "files_scanned INTEGER DEFAULT 0, new_files INTEGER DEFAULT 0, updated_files INTEGER DEFAULT 0, missing_files INTEGER DEFAULT 0, placeholders INTEGER DEFAULT 0)")
    db.init_db(c)
    assert "error" in {r[1] for r in c.execute("PRAGMA table_info(scans)")}
    db.init_db(c)  # idempotent


# ---------------------------------------------------------------- instance lock

@pytest.fixture()
def release_locks():
    yield
    for f in instance._held.values():
        f.close()
    instance._held.clear()


def test_second_instance_is_refused_with_the_holders_pid(tmp_path, release_locks):
    db_path = tmp_path / "mv.db"
    instance.acquire(db_path)

    with pytest.raises(instance.AlreadyRunning) as e:
        instance.acquire(db_path)

    assert str(os.getpid()) in str(e.value) and "mv.db" in str(e.value)
    instance.acquire(tmp_path / "other.db")     # a different database is independent


def test_lock_is_released_when_the_holder_dies(tmp_path, release_locks):
    db_path = tmp_path / "mv.db"
    code = textwrap.dedent(f"""
        import sys, time
        from pathlib import Path
        from mediavault import instance
        instance.acquire(Path({str(db_path)!r}))
        print("locked", flush=True)
        time.sleep(60)
    """)
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                         env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    try:
        assert p.stdout.readline().strip() == "locked"
        with pytest.raises(instance.AlreadyRunning, match=str(p.pid)):
            instance.acquire(db_path)
    finally:
        p.kill()
        p.wait()
    instance.acquire(db_path)     # killed -> the OS dropped the lock; no stale-lockfile cleanup needed


# ------------------------------------------------------------------ retry timing

def _worker(minutes):
    cfg = AppConfig(machine="m", db_path="x", hash_algo="sha256", tmdb_api_key_env="X",
                    video_extensions=[".mkv"], scan_interval_minutes=minutes)
    return scheduler.BackgroundWorker(cfg)


def test_failed_or_partial_cycles_are_retried_sooner_than_the_interval():
    w = _worker(60)
    assert w._next_delay({"scan_errors": []}) == 3600                       # healthy: the normal interval
    assert w._next_delay({"scan_errors": ["Network Drive: not mounted"]}) == scheduler.RETRY_AFTER_SKIPPED_ROOT_SECONDS
    assert w._next_delay(None) == scheduler.RETRY_AFTER_FAILURE_SECONDS     # the cycle itself crashed


def test_retry_is_never_longer_than_a_short_configured_interval():
    w = _worker(1)
    assert w._next_delay(None) == 60 and w._next_delay({"scan_errors": ["x"]}) == 60


def _vanish_on(*names):
    real = scanner.hash_file

    def hash_file(path, algo="sha256"):
        if path.name in names:
            path.unlink()
            raise FileNotFoundError(2, "No such file or directory")
        return real(path, algo=algo)

    return hash_file


def test_a_file_vanishing_mid_scan_does_not_abort_the_scan(conn, tmp_path, monkeypatch):
    root = make_root(tmp_path, label="lib", role="primary", mode="mirror")
    write_file(root.path, "A.mkv", b"aaa")
    write_file(root.path, "B.mkv", b"bbb")
    write_file(root.path, "C.mkv", b"ccc")
    monkeypatch.setattr(scanner, "hash_file", _vanish_on("B.mkv"))

    stats = scanner.scan_root(conn, root, hash_algo="sha256")

    assert stats["vanished"] == 1
    assert db.get_abandoned_scan(conn, "lib") is None
    assert db.get_last_finished_scan(conn, "lib")["error"] is None
    assert {f["rel_path"] for f in db.all_files(conn, root_label="lib")} == {"A.mkv", "C.mkv"}


def test_a_vanished_file_that_was_indexed_is_marked_missing(conn, tmp_path, monkeypatch):
    root = make_root(tmp_path, label="lib", role="primary", mode="mirror")
    write_file(root.path, "A.mkv", b"aaa")
    write_file(root.path, "B.mkv", b"bbb")
    scanner.scan_root(conn, root, hash_algo="sha256")
    write_file(root.path, "B.mkv", b"bbbb")                  # changed, so it gets re-hashed
    monkeypatch.setattr(scanner, "hash_file", _vanish_on("B.mkv"))

    scanner.scan_root(conn, root, hash_algo="sha256")

    assert db.get_file(conn, "lib", "B.mkv")["missing"] == 1
    assert db.get_file(conn, "lib", "A.mkv")["missing"] == 0


def test_a_flood_of_vanishing_files_still_aborts_the_scan(conn, tmp_path, monkeypatch):
    root = make_root(tmp_path, label="lib", role="primary", mode="mirror")
    names = [f"F{i:03d}.mkv" for i in range(scanner.MAX_VANISHED_PER_SCAN + 5)]
    for n in names:
        write_file(root.path, n, b"x" + n.encode())
    monkeypatch.setattr(scanner, "hash_file", _vanish_on(*names))

    with pytest.raises(FileNotFoundError):
        scanner.scan_root(conn, root, hash_algo="sha256")

    assert db.get_abandoned_scan(conn, "lib") is not None
    assert conn.execute("SELECT COUNT(*) FROM files WHERE root_label='lib' AND missing=1").fetchone()[0] == 0
