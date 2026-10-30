"""Different roots (different drives) scan side by side; the same root never
scans twice at once; a manual root scan neither waits for nor blocks the rest."""
import threading
import time

import pytest

from mediavault import db, scanner, scheduler
from mediavault.config import AppConfig

from .conftest import write_file
from .test_scanner import make_root


def make_config(tmp_path):
    return AppConfig(machine="m", db_path=tmp_path / "mediavault.db", hash_algo="sha256", tmdb_api_key_env="X",
                     video_extensions=[".mkv"], scan_interval_minutes=15)


def _roots(conn, tmp_path, n, files=3):
    out = []
    for i in range(n):
        root = make_root(tmp_path, label=f"drive{i}", role="primary" if i == 0 else "backup", mode="mirror")
        for j in range(files):
            write_file(root.path, f"Movie{i}-{j}.mkv", f"{i}-{j}".encode() * 50)
        db.upsert_root(conn, root.label, str(root.path), root.role, root.mode)
        out.append(root)
    conn.commit()
    return out


@pytest.fixture(autouse=True)
def fresh_locks():
    scheduler._root_locks.clear()
    yield
    scheduler._root_locks.clear()


def test_two_roots_are_scanned_at_the_same_time(conn, tmp_path, monkeypatch):
    """Sequential scanning would leave the first root waiting at the barrier
    for a second that never starts."""
    _roots(conn, tmp_path, 2)
    barrier = threading.Barrier(2, timeout=5)
    real = scanner.scan_root

    def scan_root(c, root, hash_algo="sha256"):
        barrier.wait()
        return real(c, root, hash_algo=hash_algo)

    monkeypatch.setattr(scanner, "scan_root", scan_root)

    errors = scheduler.scan_roots(conn, make_config(tmp_path))

    assert errors == []
    assert {f["root_label"] for f in db.all_files(conn)} == {"drive0", "drive1"}


def test_parallelism_is_capped(conn, tmp_path, monkeypatch):
    _roots(conn, tmp_path, 7, files=1)
    lock, now, peak = threading.Lock(), [0], [0]
    real = scanner.scan_root

    def scan_root(c, root, hash_algo="sha256"):
        with lock:
            now[0] += 1
            peak[0] = max(peak[0], now[0])
        time.sleep(0.1)
        try:
            return real(c, root, hash_algo=hash_algo)
        finally:
            with lock:
                now[0] -= 1

    monkeypatch.setattr(scanner, "scan_root", scan_root)
    scheduler.scan_roots(conn, make_config(tmp_path))

    assert 2 <= peak[0] <= scheduler.MAX_PARALLEL_SCANS
    assert len({f["root_label"] for f in db.all_files(conn)}) == 7


def test_a_root_that_is_already_being_scanned_is_left_to_that_scan(conn, tmp_path, monkeypatch):
    _roots(conn, tmp_path, 1)
    monkeypatch.setattr(scanner, "scan_root", lambda *a, **k: pytest.fail("scanned a root that is already being scanned"))
    with scheduler.root_lock("drive0"):
        assert scheduler.scan_roots(conn, make_config(tmp_path)) == []


def test_one_failing_root_does_not_stop_the_others_running_in_parallel(conn, tmp_path, monkeypatch):
    _roots(conn, tmp_path, 3)
    real = scanner.scan_root

    def scan_root(c, root, hash_algo="sha256"):
        if root.label == "drive1":
            raise ConnectionError("stale NFS file handle")
        return real(c, root, hash_algo=hash_algo)

    monkeypatch.setattr(scanner, "scan_root", scan_root)

    errors = scheduler.scan_roots(conn, make_config(tmp_path))

    assert len(errors) == 1 and "stale NFS" in errors[0]
    assert {f["root_label"] for f in db.all_files(conn)} == {"drive0", "drive2"}


def test_parallel_scans_alongside_other_database_writes_never_hit_a_lock_error(conn, tmp_path):
    """The reason scans used to be serialized. Three real scans with hundreds
    of files write through their own connections while the web UI's kind of
    write (worker_state, a quarantine record) keeps going."""
    _roots(conn, tmp_path, 3, files=150)
    cfg = make_config(tmp_path)
    stop, failures, writes = threading.Event(), [], [0]

    def web_writer():
        try:
            while not stop.is_set():
                with db.connect(cfg.db_path) as c:
                    db.set_worker_state(c, "ui_pokes", writes[0])
                writes[0] += 1
        except Exception as exc:  # noqa: BLE001
            failures.append(exc)

    t = threading.Thread(target=web_writer)
    t.start()
    try:
        errors = scheduler.scan_roots(conn, cfg)
    finally:
        stop.set()
        t.join()

    assert errors == [] and failures == [] and writes[0] > 0
    assert len(db.all_files(conn)) == 450


# --------------------------------------------------------------------- worker

def test_a_root_scan_runs_while_a_full_cycle_is_busy(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    with db.connect(cfg.db_path) as c:
        db.init_db(c)
    w = scheduler.BackgroundWorker(cfg)
    in_cycle, release, scanned = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(w, "_run_cycle", lambda: (in_cycle.set(), release.wait(5), {"scan_errors": []})[2])
    monkeypatch.setattr(scheduler, "scan_roots", lambda conn, config, only=None: (scanned.set(), [])[1])
    w.start()
    try:
        assert in_cycle.wait(3)
        assert w.request_run(["Network Drive"]) is True       # not blocked by the running cycle
        assert scanned.wait(3)
        assert w.request_run(None) is False                   # but a second *full* scan still is
    finally:
        release.set()
        w.stop()


def test_the_same_root_cannot_be_started_twice(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    with db.connect(cfg.db_path) as c:
        db.init_db(c)
    w = scheduler.BackgroundWorker(cfg)
    inside, release = threading.Event(), threading.Event()
    monkeypatch.setattr(scheduler, "scan_roots", lambda conn, config, only=None: (inside.set(), release.wait(5), [])[2])
    assert w.request_run(["Passport"]) is True and inside.wait(3)
    assert w.request_run(["Passport"]) is False               # already being scanned
    assert w.request_run(["Passport", "iCloud"]) is True      # a different root still starts
    release.set()
    for _ in range(100):
        if not w.is_busy():
            break
        time.sleep(0.02)
    assert not w.is_busy()


def test_a_full_scan_can_be_requested_while_a_root_scan_runs(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    w = scheduler.BackgroundWorker(cfg)
    inside, release = threading.Event(), threading.Event()
    monkeypatch.setattr(scheduler, "scan_roots", lambda conn, config, only=None: (inside.set(), release.wait(5), [])[2])
    with db.connect(cfg.db_path) as c:
        db.init_db(c)
    w.request_run(["Passport"])
    assert inside.wait(3)
    assert w.request_run(None) is True        # the cycle will skip Passport (locked) and scan the rest
    release.set()
