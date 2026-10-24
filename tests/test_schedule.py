"""Scan schedule: the user-chosen interval, last/next run tracking, advice on
whether a frequency is shorter than a scan takes, and the worker honouring
manual runs and interval changes."""
import threading
import time
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from mediavault import db, recycle_bin, schedule, scheduler
from mediavault.config import AppConfig
from mediavault.web import app as web_app

from .conftest import write_file
from .test_scanner import make_root


def make_config(tmp_path, minutes=60):
    return AppConfig(machine="m", db_path=tmp_path / "mv.db", hash_algo="sha256", tmdb_api_key_env="X",
                     video_extensions=[".mkv"], scan_interval_minutes=minutes)


# ----------------------------------------------------------------- interval

def test_interval_defaults_to_config_until_the_user_picks_one(conn, tmp_path):
    cfg = make_config(tmp_path, minutes=45)
    assert schedule.get_interval_minutes(conn, cfg) == 45
    schedule.set_interval_minutes(conn, 180)
    assert schedule.get_interval_minutes(conn, cfg) == 180


@pytest.mark.parametrize("bad", [0, 4, 1441, -5, "soon", None])
def test_interval_is_validated(conn, bad):
    with pytest.raises(ValueError):
        schedule.set_interval_minutes(conn, bad)


def test_a_wild_config_value_is_clamped(conn, tmp_path):
    assert schedule.get_interval_minutes(conn, make_config(tmp_path, minutes=1)) == schedule.MIN_MINUTES
    assert schedule.get_interval_minutes(conn, make_config(tmp_path, minutes=99999)) == schedule.MAX_MINUTES


@pytest.mark.parametrize("minutes,label", [(15, "15 minutes"), (60, "1 hour"), (180, "3 hours"), (90, "90 minutes"), (240, "4 hours")])
def test_labels(minutes, label):
    assert schedule.label_for(minutes) == label


# ------------------------------------------------------------------- timing

def _scan(conn, label, start, seconds, error=None):
    conn.execute("INSERT INTO scans (root_label, started_at, finished_at, error) VALUES (?, ?, ?, ?)", (label, start, start + seconds, error))


def test_estimates_use_the_median_of_recent_scans_not_the_one_off_first_scan(conn):
    db.upsert_root(conn, "Big", "/x", "backup", "mirror")
    db.upsert_root(conn, "Small", "/y", "primary", "mirror")
    for i, secs in enumerate([5400, 120, 130, 125, 118]):       # first scan hashed everything: 90 min; since then ~2 min
        _scan(conn, "Big", 1000 + i * 10000, secs)
    _scan(conn, "Small", 1000, 30)
    _scan(conn, "Big", 99999, 4000, error="OSError: dropped")      # failed scans say nothing about duration

    est = schedule.scan_estimates(conn)

    assert est["largest_label"] == "Big" and 100 < est["largest_seconds"] < 200
    assert est["per_root"]["Small"] == 30


def test_no_data_means_no_advice(conn):
    est = schedule.scan_estimates(conn)
    assert est["largest_seconds"] is None
    assert schedule.advise(15, est) == {"level": "ok", "message": "", "suggested_minutes": None}


def test_cycle_start_end_and_next_run_are_recorded(conn):
    schedule.record_cycle_start(conn, now=1000.0)
    assert schedule.read_state(conn)["running"] is True
    schedule.record_cycle_end(conn, 1000.0, ok=True, scan_errors=1, now=1180.0)
    schedule.set_next_run(conn, 4780.0, "retry")

    s = schedule.read_state(conn)

    assert (s["running"], s["last_finished_at"], s["last_duration"], s["last_scan_errors"]) == (False, 1180.0, 180.0, 1)
    assert (s["next_run_at"], s["next_reason"]) == (4780.0, "retry")


# ------------------------------------------------------------------- advice

def _est(largest=2400, label="Network Drive", cycle=None):
    return {"largest_seconds": largest, "largest_label": label, "cycle_seconds": cycle}


def test_an_interval_shorter_than_the_largest_drives_scan_is_a_warning():
    a = schedule.advise(15, _est(largest=2400))                       # 40 min scan, every 15 min
    assert a["level"] == "warn"
    assert "largest drive (Network Drive) takes about 40 min" in a["message"] and "back-to-back" in a["message"]
    assert a["suggested_minutes"] == 60


def test_the_whole_cycle_counts_too():
    assert schedule.advise(60, _est(largest=600, cycle=4000))["level"] == "warn"      # drives scan in turn: 67 min total


def test_message_distinguishes_the_whole_pass_from_the_largest_drive():
    a = schedule.advise(15, _est(largest=1200, cycle=3000))
    assert "all your drives" in a["message"] and "50 min" in a["message"] and "Network Drive" in a["message"] and "20 min" in a["message"]


def test_a_tight_interval_is_only_a_note():
    assert schedule.advise(60, _est(largest=2400))["level"] == "note"                  # 40 min scan every 60: little rest


def test_a_roomy_interval_is_fine():
    a = schedule.advise(360, _est(largest=2400))
    assert a["level"] == "ok" and a["suggested_minutes"] is None


def test_suggestion_is_the_next_preset_with_headroom():
    assert schedule.advise(15, _est(largest=30 * 60))["suggested_minutes"] == 60       # 45 min needed -> 1 hour
    assert schedule.advise(15, _est(largest=3 * 3600))["suggested_minutes"] == 360


# ------------------------------------------------------------------- worker

def test_cycle_run_records_last_finished_and_duration(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    with db.connect(cfg.db_path) as c:
        db.init_db(c)
    w = scheduler.BackgroundWorker(cfg)
    monkeypatch.setattr(w, "_run_cycle", lambda: {"scan_errors": []})

    w._run(None)

    with db.connect(cfg.db_path) as c:
        s = schedule.read_state(c)
    assert s["running"] is False and s["last_ok"] is True and s["last_finished_at"] >= s["started_at"]


def test_a_crashed_cycle_is_recorded_as_failed_and_retried_soon(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, minutes=120)
    with db.connect(cfg.db_path) as c:
        db.init_db(c)
    w = scheduler.BackgroundWorker(cfg)
    monkeypatch.setattr(w, "_run_cycle", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    summary = w._run(None)
    w._plan_next(summary)

    with db.connect(cfg.db_path) as c:
        s = schedule.read_state(c)
    assert s["last_ok"] is False and s["running"] is False
    assert s["next_reason"] == "retry" and s["next_run_at"] - time.time() <= scheduler.RETRY_AFTER_FAILURE_SECONDS + 1


def test_next_run_uses_the_users_interval_not_just_the_config(tmp_path):
    cfg = make_config(tmp_path, minutes=15)
    with db.connect(cfg.db_path) as c:
        db.init_db(c)
        schedule.set_interval_minutes(c, 180)
    w = scheduler.BackgroundWorker(cfg)

    w._plan_next({"scan_errors": []}, base=10_000.0)

    with db.connect(cfg.db_path) as c:
        assert schedule.read_state(c)["next_run_at"] == 10_000.0 + 180 * 60


def test_manual_request_is_refused_while_a_scan_is_running(tmp_path):
    w = scheduler.BackgroundWorker(make_config(tmp_path))
    assert w.request_run(None) is True and w.request_run(None) is False      # already queued
    w2 = scheduler.BackgroundWorker(make_config(tmp_path))
    with w2._running:
        assert w2.request_run(["x"]) is False


def test_manual_run_triggers_a_cycle_now_and_a_label_run_scans_only_that_root(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, minutes=1440)
    with db.connect(cfg.db_path) as c:
        db.init_db(c)
    w = scheduler.BackgroundWorker(cfg)
    cycles, scoped = [], []
    first, second, third = threading.Event(), threading.Event(), threading.Event()

    def cycle():
        cycles.append(1)
        [first, second][min(len(cycles) - 1, 1)].set()
        return {"scan_errors": []}

    monkeypatch.setattr(w, "_run_cycle", cycle)
    monkeypatch.setattr(scheduler, "scan_roots", lambda conn, config, only=None: (scoped.append(only), third.set(), [])[2])
    w.start()
    try:
        assert first.wait(3)                      # start-up scan
        for _ in range(100):                      # wait until idle (next run is a day away)
            if not w.is_busy():
                break
            time.sleep(0.02)
        assert w.request_run(None) and second.wait(3)         # "Scan all now"
        for _ in range(100):
            if not w.is_busy():
                break
            time.sleep(0.02)
        assert w.request_run(["Network Drive"]) and third.wait(3)
        assert scoped == [["Network Drive"]] and len(cycles) == 2
    finally:
        w.stop()


def test_changing_the_interval_moves_the_next_run(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, minutes=1440)
    with db.connect(cfg.db_path) as c:
        db.init_db(c)
    w = scheduler.BackgroundWorker(cfg)
    done = threading.Event()
    monkeypatch.setattr(w, "_run_cycle", lambda: (done.set(), {"scan_errors": []})[1])
    w.start()
    try:
        assert done.wait(3)
        time.sleep(0.2)
        with db.connect(cfg.db_path) as c:
            before = schedule.read_state(c)["next_run_at"]
            schedule.set_interval_minutes(c, 60)
        w.reschedule()
        for _ in range(100):
            with db.connect(cfg.db_path) as c:
                after = schedule.read_state(c)["next_run_at"]
            if after != before:
                break
            time.sleep(0.05)
        assert before - after > 20 * 3600          # was ~24 h out, now ~1 h
    finally:
        w.stop()


# ---------------------------------------------------------------------- web

class FakeWorker:
    def __init__(self, ok=True):
        self.ok, self.runs, self.resched = ok, [], 0

    def request_run(self, labels=None):
        self.runs.append(labels)
        return self.ok

    def reschedule(self):
        self.resched += 1


@pytest.fixture()
def client(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    with db.connect(cfg.db_path) as conn:
        db.init_db(conn)
        db.upsert_root(conn, "Net", str(tmp_path / "net"), "backup", "mirror")
        _scan(conn, "Net", 1000, 2400)                       # a 40 minute scan
    web_app.configure(cfg)
    web_app.app.state.worker = FakeWorker()
    monkeypatch.setattr(recycle_bin, "scan_trash", lambda top_n=50: {"items": [], "total_bytes": 0, "accurate": True, "access_errors": []})
    yield TestClient(web_app.app, base_url="http://127.0.0.1:8420"), cfg
    web_app.app.state.worker = None


def _flash(r):
    return urllib.parse.unquote(urllib.parse.urlparse(r.headers["location"]).query.removeprefix("msg=")).split("#")[0]


def test_scan_all_now_queues_a_run_and_returns_immediately(client):
    c, _ = client
    r = c.post("/schedule/run", follow_redirects=False)
    assert web_app.app.state.worker.runs == [None] and "Scan started" in _flash(r)


def test_scan_all_now_says_so_when_one_is_already_running(client):
    c, _ = client
    web_app.app.state.worker = FakeWorker(ok=False)
    assert "already running" in _flash(c.post("/schedule/run", follow_redirects=False))


def test_single_root_scan_goes_through_the_worker_too(client):
    c, _ = client
    r = c.post("/roots/Net/scan", follow_redirects=False)
    assert web_app.app.state.worker.runs == [["Net"]] and "background" in _flash(r)


def test_saving_an_interval_stores_it_tells_the_worker_and_warns_when_too_short(client):
    c, cfg = client
    r = c.post("/schedule/interval", data={"minutes": "15"}, follow_redirects=False)

    msg = _flash(r)
    assert "every 15 minutes" in msg and "back-to-back" in msg           # 15 min < the 40 min scan
    assert web_app.app.state.worker.resched == 1
    with db.connect(cfg.db_path) as conn:
        assert schedule.get_interval_minutes(conn, cfg) == 15


def test_a_sensible_interval_saves_without_a_warning(client):
    c, _ = client
    msg = _flash(c.post("/schedule/interval", data={"minutes": "360"}, follow_redirects=False))
    assert "every 6 hours" in msg and "back-to-back" not in msg


def test_a_bad_interval_is_refused(client):
    c, cfg = client
    assert "between" in _flash(c.post("/schedule/interval", data={"minutes": "2"}, follow_redirects=False))
    with db.connect(cfg.db_path) as conn:
        assert schedule.get_interval_minutes(conn, cfg) == 60


def test_advice_endpoint_previews_before_saving(client):
    c, _ = client
    assert c.get("/api/schedule/advice?minutes=15").json()["level"] == "warn"
    assert c.get("/api/schedule/advice?minutes=720").json()["level"] == "ok"


def test_poll_and_page_carry_the_schedule(client):
    c, cfg = client
    with db.connect(cfg.db_path) as conn:
        schedule.record_cycle_end(conn, 1000.0, True, 0, now=1090.0)
        schedule.set_next_run(conn, time.time() + 1800, "")
    data = c.get("/api/ui-state?counts=0").json()["schedule"]
    assert data["interval_minutes"] == 60 and data["last_duration"] == 90.0 and data["advice"]["level"] == "note"
    page = c.get("/").text
    assert 'id="sched-last"' in page and 'id="sched-next"' in page and "Scan all now" in page
    assert 'name="minutes"' in page and "in 30 min" in page and "took 2 min" in page
