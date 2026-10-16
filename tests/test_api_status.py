"""/api/status is polled by an out-of-repo consumer (Donna's hub), which does
arithmetic on `last_scan` directly. Twice now it broke that consumer:
first by returning a raw sqlite3.Row (500, "not JSON serializable"), then --
the "fix" for that -- by returning a dict instead of a timestamp, which made
the hub's `time.time() - last_scan` raise TypeError and drop every request to
its MediaVault page. These tests exercise the real route and pin the types
consumers depend on, including doing the same arithmetic the hub does.
"""
import time

import pytest
from fastapi.testclient import TestClient

from mediavault import db, scanner
from mediavault.config import AppConfig
from mediavault.web import app as web_app

from .conftest import write_file
from .test_scanner import make_root


@pytest.fixture()
def client(tmp_path):
    config = AppConfig(
        machine="test-machine", db_path=tmp_path / "mediavault.db", hash_algo="sha256",
        tmdb_api_key_env="X", video_extensions=[".mkv"], scan_interval_minutes=15,
    )
    with db.connect(config.db_path) as conn:
        db.init_db(conn)
    web_app.configure(config)
    # No `with TestClient(...)`: that would run the lifespan and start the
    # real background scanner. The route doesn't need it.
    return TestClient(web_app.app, base_url="http://127.0.0.1:8420"), config


def test_never_scanned_root_reports_null_last_scan(client):
    c, config = client
    with db.connect(config.db_path) as conn:
        db.upsert_root(conn, "primary", "/nowhere", "primary", "mirror")

    r = c.get("/api/status")

    assert r.status_code == 200
    root = r.json()["roots"][0]
    assert root["last_scan"] is None
    assert root["last_scan_files"] is None
    assert root["scanning"] is False


def test_finished_scan_reports_a_numeric_timestamp_consumers_can_subtract(client, tmp_path):
    c, config = client
    root_cfg = make_root(tmp_path, label="primary", role="primary", mode="mirror")
    write_file(root_cfg.path, "Movie.mkv", b"content")
    with db.connect(config.db_path) as conn:
        scanner.scan_root(conn, root_cfg, hash_algo="sha256")

    r = c.get("/api/status")

    assert r.status_code == 200
    root = r.json()["roots"][0]
    assert isinstance(root["last_scan"], float)
    assert time.time() - root["last_scan"] >= 0  # exactly what the hub does
    assert root["last_scan_files"] == 1


def test_top_level_shape_is_stable(client):
    c, _ = client
    body = c.get("/api/status").json()
    assert set(body) >= {
        "machine", "roots", "total_files", "total_bytes", "duplicate_groups",
        "wasted_bytes", "untracked_count", "quarantine_count", "any_scan_active",
    }
