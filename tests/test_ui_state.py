"""The live UI: per-root state words, the cheap poll endpoint, and the page
shell (sidebar badges, status strip) that the poll keeps up to date."""
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from mediavault import db, recycle_bin, scanner
from mediavault.config import AppConfig
from mediavault.web import app as web_app

from .conftest import write_file
from .test_scanner import make_root


def _row(**kw):
    return {"label": "R", "path": "/x/R", "enabled": 1, **kw}


def _status(active=None, last=None, abandoned=None):
    return {"active": active, "last_finished": last, "abandoned": abandoned}


def test_describe_root_covers_every_state():
    d = web_app.describe_root
    assert d(_row(enabled=0), _status(), True)["state"] == "disabled"

    scanning = d(_row(), _status(active={"files_scanned": 1234}), True)
    assert scanning["state"] == "scanning" and "1,234" in scanning["text"]

    offline = d(_row(), _status(last={"finished_at": time.time() - 7200, "files_scanned": 5}), False)
    assert offline["state"] == "offline" and "isn't reachable" in offline["text"] and "2h ago" in offline["text"]

    failed = d(_row(), _status(abandoned={"files_scanned": 40, "error": "OSError: Input/output error"}), True)
    assert failed["state"] == "failed" and "40" in failed["text"] and "Input/output error" in failed["detail"]

    ok = d(_row(), _status(last={"finished_at": time.time(), "files_scanned": 9}), True)
    assert ok["state"] == "ok" and "9 file(s)" in ok["text"]

    assert d(_row(), _status(), True)["state"] == "never"


def test_offline_beats_failed_and_scanning_beats_offline():
    d = web_app.describe_root
    both = d(_row(), _status(abandoned={"files_scanned": 1, "error": None}), False)
    assert both["state"] == "offline"          # the drive being gone is the thing to tell the user
    assert d(_row(), _status(active={"files_scanned": 3}), False)["state"] == "scanning"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    config = AppConfig(
        machine="test-machine", db_path=tmp_path / "mediavault.db", hash_algo="sha256",
        tmdb_api_key_env="X", video_extensions=[".mkv"], scan_interval_minutes=15,
    )
    with db.connect(config.db_path) as conn:
        db.init_db(conn)
    web_app.configure(config)
    monkeypatch.setattr(
        recycle_bin, "scan_trash",
        lambda top_n=50: {"items": [], "total_bytes": 0, "accurate": True, "access_errors": []},
    )
    return TestClient(web_app.app), config


def _library(conn, tmp_path):
    primary = make_root(tmp_path, label="primary", role="primary", mode="mirror")
    backup = make_root(tmp_path, label="backup", role="backup", mode="mirror")
    write_file(primary.path, "Movie.mkv", b"same bytes")
    write_file(primary.path, "Elsewhere/Movie copy.mkv", b"same bytes")
    write_file(primary.path, "Only.mkv", b"only on primary")
    scanner.scan_root(conn, primary, hash_algo="sha256")
    scanner.scan_root(conn, backup, hash_algo="sha256")
    return primary, backup


def test_poll_endpoint_reports_roots_and_counts_as_plain_json(client, tmp_path):
    c, config = client
    with db.connect(config.db_path) as conn:
        _library(conn, tmp_path)

    r = c.get("/api/ui-state")

    assert r.status_code == 200
    data = r.json()                                   # serializable: no sqlite3.Row leaked through
    roots = {x["label"]: x for x in data["roots"]}
    assert roots["primary"]["state"] == "ok" and isinstance(roots["primary"]["scanned_at"], float)
    assert data["any_scan_active"] is False
    assert data["counts"]["duplicates"] == 1
    assert data["counts"]["sync"] == 1                # backup is missing files primary has
    assert set(data["counts"]) >= {"duplicates", "sync", "promote", "suggestions", "peers", "recycle-bin"}


def test_poll_endpoint_skips_the_heavy_counts_on_request(client, tmp_path):
    c, config = client
    with db.connect(config.db_path) as conn:
        _library(conn, tmp_path)
    data = c.get("/api/ui-state?counts=0").json()
    assert "counts" not in data and data["roots"]


def test_poll_notices_a_drive_going_away(client, tmp_path):
    c, config = client
    with db.connect(config.db_path) as conn:
        primary, backup = _library(conn, tmp_path)
    shutil.rmtree(backup.path)

    states = {x["label"]: x["state"] for x in c.get("/api/ui-state?counts=0").json()["roots"]}

    assert states == {"primary": "ok", "backup": "offline"}


def test_page_has_sidebar_status_strip_and_no_meta_refresh(client, tmp_path):
    c, config = client
    with db.connect(config.db_path) as conn:
        primary, backup = _library(conn, tmp_path)
    shutil.rmtree(backup.path)

    page = c.get("/").text

    assert 'http-equiv="refresh"' not in page                       # replaced by the polling fetch
    assert '<script src="/static/live.js"' in page
    assert 'href="/#duplicates"' in page and 'data-count="duplicates"' in page
    assert 'data-root-chip="backup"' in page and 'class="chip offline"' in page
    assert 'class="root-card offline" data-root="backup"' in page
    assert 'data-attn-chip="duplicates"' in page
    # the actions the old table had are all still there
    for action in ("/roots/primary/scan", "/roots/primary/toggle", "/roots/primary/update", "/roots/primary/delete"):
        assert f'action="{action}"' in page


def test_attention_chips_are_hidden_when_nothing_needs_attention(client):
    c, _ = client
    page = c.get("/").text
    assert 'data-attn-chip="duplicates" hidden' in page
    assert 'data-count="duplicates" hidden' in page


def test_static_assets_are_served(client):
    c, _ = client
    assert c.get("/static/live.js").status_code == 200
    assert "ui-state" in c.get("/static/live.js").text
