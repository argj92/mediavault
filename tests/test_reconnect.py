"""A network-drive root that drops off: where to re-mount it from, finding it
again under a different mount name, and retrying on its own (throttled)."""
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from mediavault import db, reconnect, recycle_bin, scheduler
from mediavault.config import AppConfig
from mediavault.web import app as web_app


@pytest.mark.parametrize("url", ["smb://nas.local/Videos", "SMB://user@nas/Share%20Name", "afp://nas/x", "nfs://10.0.0.5/export"])
def test_network_share_urls_are_accepted(url):
    assert reconnect.validate_mount_url(f"  {url}\n") == url


@pytest.mark.parametrize("url", [
    "", "file:///Applications/Calculator.app", "/Volumes/x", "javascript:alert(1)", "https://example.com",
    "smb://", "smb://nas/has space", "x-apple.systempreferences:", "smb://nas/a\nb",
])
def test_anything_else_is_refused_because_open_launches_whatever_it_gets(url):
    with pytest.raises(ValueError):
        reconnect.validate_mount_url(url)


def test_finds_the_share_when_macos_mounted_it_under_a_new_name(tmp_path):
    vols = tmp_path / "Volumes"
    (vols / "akashjain-1" / "External Data" / "Videos").mkdir(parents=True)
    (vols / "akashjain-2" / "Other").mkdir(parents=True)                 # same share name, wrong content
    (vols / "akashjain-notanumber" / "External Data" / "Videos").mkdir(parents=True)
    (vols / "different-1" / "External Data" / "Videos").mkdir(parents=True)

    found = reconnect.candidate_paths(vols / "akashjain" / "External Data" / "Videos", volumes=vols)

    assert found == [vols / "akashjain-1" / "External Data" / "Videos"]


def test_no_candidates_for_paths_outside_volumes(tmp_path):
    assert reconnect.candidate_paths(tmp_path / "x", volumes=tmp_path / "Volumes") == []


def test_reconnect_asks_macos_to_open_the_url_and_reports_success(tmp_path, monkeypatch):
    calls = []
    target = tmp_path / "mounted"
    monkeypatch.setattr(reconnect.platform, "system", lambda: "Darwin")

    def fake_run(cmd, **kw):
        calls.append(cmd)
        target.mkdir()                      # the share appears
    monkeypatch.setattr(reconnect.subprocess, "run", fake_run)

    ok, msg = reconnect.reconnect("smb://nas/Videos", target, wait_seconds=2)

    assert ok and calls == [["open", "smb://nas/Videos"]] and msg == "Connected."


def test_reconnect_tells_the_user_about_the_login_box_when_it_does_not_appear(tmp_path, monkeypatch):
    monkeypatch.setattr(reconnect.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(reconnect.subprocess, "run", lambda *a, **k: None)
    ok, msg = reconnect.reconnect("smb://nas/Videos", tmp_path / "never", wait_seconds=0.1)
    assert not ok and "keychain" in msg and "password" in msg


def test_reconnect_refuses_a_bad_url_before_running_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(reconnect.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    with pytest.raises(ValueError):
        reconnect.reconnect("file:///etc", tmp_path)


def _root(conn, tmp_path, url="smb://nas/Videos"):
    db.upsert_root(conn, "Net", str(tmp_path / "gone"), "backup", "mirror")
    db.set_root_mount_url(conn, "Net", url)
    return db.get_root(conn, "Net")


def test_auto_reconnect_is_throttled(conn, tmp_path, monkeypatch):
    row = _root(conn, tmp_path)
    attempts = []
    monkeypatch.setattr(reconnect, "reconnect", lambda url, path, wait_seconds=0: attempts.append(url) or (False, ""))

    reconnect.try_auto(conn, row, now=1000.0)
    reconnect.try_auto(conn, row, now=1000.0 + 60)                       # too soon: no login-dialog spam
    reconnect.try_auto(conn, row, now=1000.0 + reconnect.AUTO_RETRY_SECONDS + 1)

    assert len(attempts) == 2


def test_auto_reconnect_needs_a_saved_address(conn, tmp_path, monkeypatch):
    row = _root(conn, tmp_path, url=None)
    monkeypatch.setattr(reconnect, "reconnect", lambda *a, **k: pytest.fail("no address saved"))
    assert reconnect.try_auto(conn, row) is False


def test_background_scan_tries_to_bring_an_unreachable_root_back(conn, tmp_path, monkeypatch):
    _root(conn, tmp_path)
    tried = []
    monkeypatch.setattr(reconnect, "try_auto", lambda c, row, now=None: tried.append(row["label"]) or False)
    config = AppConfig(machine="m", db_path=tmp_path / "mediavault.db", hash_algo="sha256", tmdb_api_key_env="X",
                       video_extensions=[".mkv"], scan_interval_minutes=15)

    summary = scheduler.reconcile_once(conn, config)

    assert tried == ["Net"] and any("does not exist" in e for e in summary["scan_errors"])


# --------------------------------------------------------------------- web

@pytest.fixture()
def client(tmp_path, monkeypatch):
    config = AppConfig(machine="test-machine", db_path=tmp_path / "mediavault.db", hash_algo="sha256",
                       tmdb_api_key_env="X", video_extensions=[".mkv"], scan_interval_minutes=15)
    with db.connect(config.db_path) as conn:
        db.init_db(conn)
        _root(conn, tmp_path)
    web_app.configure(config)
    monkeypatch.setattr(recycle_bin, "scan_trash", lambda top_n=50: {"items": [], "total_bytes": 0, "accurate": True, "access_errors": []})
    return TestClient(web_app.app, base_url="http://127.0.0.1:8420"), config


def _flash(r):
    return urllib.parse.unquote(urllib.parse.urlparse(r.headers["location"]).query.removeprefix("msg="))


def test_save_and_clear_the_network_address(client):
    c, config = client
    r = c.post("/roots/Net/mount-url", data={"mount_url": "smb://nas.local/Other"}, follow_redirects=False)
    assert "Saved" in _flash(r)
    with db.connect(config.db_path) as conn:
        assert db.get_root(conn, "Net")["mount_url"] == "smb://nas.local/Other"
    c.post("/roots/Net/mount-url", data={"mount_url": ""}, follow_redirects=False)
    with db.connect(config.db_path) as conn:
        assert db.get_root(conn, "Net")["mount_url"] is None


def test_a_non_share_address_is_refused_and_not_saved(client):
    c, config = client
    r = c.post("/roots/Net/mount-url", data={"mount_url": "file:///Applications/Calculator.app"}, follow_redirects=False)
    assert "network share address" in _flash(r)
    with db.connect(config.db_path) as conn:
        assert db.get_root(conn, "Net")["mount_url"] == "smb://nas/Videos"        # unchanged


def test_reconnect_route_uses_only_the_saved_address(client, monkeypatch):
    c, _ = client
    seen = []
    monkeypatch.setattr(reconnect, "reconnect", lambda url, path, wait_seconds=8.0: seen.append((url, path)) or (False, "asked"))
    r = c.post("/roots/Net/reconnect", data={"mount_url": "smb://attacker/evil"}, follow_redirects=False)
    assert seen[0][0] == "smb://nas/Videos" and _flash(r) == "asked"


def test_reconnect_without_a_saved_address_explains_what_to_do(client):
    c, config = client
    with db.connect(config.db_path) as conn:
        db.set_root_mount_url(conn, "Net", None)
    assert "Network login" in _flash(c.post("/roots/Net/reconnect", follow_redirects=False))


def test_use_path_only_accepts_a_place_the_server_found(client, monkeypatch, tmp_path):
    c, config = client
    good = tmp_path / "Volumes" / "x-1" / "Videos"
    monkeypatch.setattr(reconnect, "candidate_paths", lambda p: [good])
    r = c.post("/roots/Net/use-path", data={"path": "/etc"}, follow_redirects=False)
    assert "nothing changed" in _flash(r)
    c.post("/roots/Net/use-path", data={"path": str(good)}, follow_redirects=False)
    with db.connect(config.db_path) as conn:
        assert db.get_root(conn, "Net")["path"] == str(good)


def test_offline_card_offers_reconnect_and_the_found_path(client, monkeypatch, tmp_path):
    c, _ = client
    monkeypatch.setattr(reconnect, "candidate_paths", lambda p: [tmp_path / "Volumes" / "gone-1"])
    page = c.get("/").text
    assert 'action="/roots/Net/reconnect"' in page and "smb://nas/Videos" in page
    assert 'action="/roots/Net/use-path"' in page and "gone-1" in page
    assert 'action="/roots/Net/mount-url"' in page


def test_init_db_adds_mount_url_to_an_existing_roots_table(tmp_path):
    import sqlite3
    c = sqlite3.connect(tmp_path / "old.db")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE roots (label TEXT PRIMARY KEY, path TEXT NOT NULL, role TEXT NOT NULL, mode TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL DEFAULT 0)")
    db.init_db(c)
    assert "mount_url" in {r[1] for r in c.execute("PRAGMA table_info(roots)")}
