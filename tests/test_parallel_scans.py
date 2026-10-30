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


# ------------------------------------------- a blocked walk must not hold the lock

def _slow_walk(monkeypatch, slow_root_path, unblock: threading.Event):
    """Walks that stall mid-way (a huge network folder listing) for one root."""
    from mediavault import cloud as cloud_mod
    real = cloud_mod.walk_cloud_aware

    def walk(root):
        entries = real(root)
        if root != slow_root_path:
            yield from entries
            return
        for i, e in enumerate(entries):
            yield e
            if i == 1:
                unblock.wait(8)          # stalls inside the walk, after two files were upserted

    monkeypatch.setattr(cloud_mod, "walk_cloud_aware", walk)


def test_a_scan_stalled_inside_its_walk_does_not_hold_the_database_write_lock(tmp_path, monkeypatch):
    """The live bug: a slow Network Drive listing kept its scan's write
    transaction open, so every other scan waited on it -- 'parallel' scans ran
    one after another."""
    cfg = make_config(tmp_path)
    with db.connect(cfg.db_path) as c:
        db.init_db(c)
        slow, = _roots(c, tmp_path, 1, files=5)
    unblock = threading.Event()
    _slow_walk(monkeypatch, slow.path, unblock)

    def run_scan():
        with db.connect(cfg.db_path) as c2:
            scanner.scan_root(c2, slow, hash_algo="sha256")

    t = threading.Thread(target=run_scan)
    t.start()
    try:
        time.sleep(0.3)                                  # scan is now stalled mid-walk
        started = time.monotonic()
        with db.connect(cfg.db_path) as other:
            db.set_worker_state(other, "poke", 1)        # any other writer: another root's scan, the web UI
        waited = time.monotonic() - started
    finally:
        unblock.set()
        t.join(10)

    assert waited < 2.0, f"another writer waited {waited:.1f}s behind a scan stalled in its walk"


def test_a_fast_root_finishes_while_a_slow_one_is_stalled_in_its_walk(conn, tmp_path, monkeypatch):
    slow, fast = _roots(conn, tmp_path, 2, files=5)
    unblock = threading.Event()
    _slow_walk(monkeypatch, slow.path, unblock)
    real = scanner.scan_root

    def scan_root(c, root, hash_algo="sha256"):
        out = real(c, root, hash_algo=hash_algo)
        if root.label == fast.label:
            unblock.set()            # only the fast root finishing lets the slow one continue
        return out

    monkeypatch.setattr(scanner, "scan_root", scan_root)
    started = time.monotonic()

    errors = scheduler.scan_roots(conn, make_config(tmp_path))

    assert errors == [] and time.monotonic() - started < 6
    assert {f["root_label"] for f in db.all_files(conn)} == {"drive0", "drive1"}


def test_walk_errors_still_surface_with_the_walk_on_another_thread(conn, tmp_path, monkeypatch):
    from mediavault import cloud as cloud_mod
    root, = _roots(conn, tmp_path, 1)

    def boom(path):
        yield from ()
        raise ConnectionError("stale NFS file handle")

    monkeypatch.setattr(cloud_mod, "walk_cloud_aware", boom)
    with pytest.raises(ConnectionError, match="stale NFS"):
        scanner.scan_root(conn, root, hash_algo="sha256")
    assert db.get_abandoned_scan(conn, "drive0")["error"].startswith("ConnectionError")
