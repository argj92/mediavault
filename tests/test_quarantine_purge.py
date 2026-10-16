"""Purging quarantine: the one irreversible step, so it's explicit and tested."""
import os
import time
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from mediavault import db, dedupe, recycle_bin, scanner
from mediavault.config import AppConfig
from mediavault.web import app as web_app

from .conftest import write_file
from .test_scanner import make_root


def _quarantined(conn, tmp_path, n=2):
    root = make_root(tmp_path, label="lib", role="inbox", mode="watch")
    for i in range(n):
        write_file(root.path, f"dup{i}.mkv", b"x" * (100 + i))
    scanner.scan_root(conn, root, hash_algo="sha256")
    qdir = tmp_path / "q"
    return [dedupe.quarantine_duplicate(conn, qdir, root.path, "lib", f"dup{i}.mkv") for i in range(n)]


def _age(conn, days, qid=None):
    conn.execute("UPDATE quarantine SET quarantined_at = ? WHERE (? IS NULL OR id = ?)", (time.time() - days * 86400, qid, qid))


def test_purge_all_deletes_files_and_records(conn, tmp_path):
    paths = _quarantined(conn, tmp_path)

    res = dedupe.purge_quarantine(conn)

    assert res == {"purged": 2, "bytes": 201, "errors": []}
    assert not any(p.exists() for p in paths) and db.list_quarantine(conn) == []


def test_purge_older_than_keeps_recent_items(conn, tmp_path):
    paths = _quarantined(conn, tmp_path)
    old_id = db.list_quarantine(conn)[0]["id"]
    _age(conn, 45, old_id)

    res = dedupe.purge_quarantine(conn, older_than_days=30)

    assert res["purged"] == 1
    remaining = db.list_quarantine(conn)
    assert len(remaining) == 1 and remaining[0]["id"] != old_id
    assert sum(p.exists() for p in paths) == 1


def test_purge_tolerates_a_file_already_gone(conn, tmp_path):
    paths = _quarantined(conn, tmp_path, n=1)
    paths[0].unlink()
    assert dedupe.purge_quarantine(conn)["purged"] == 1 and db.list_quarantine(conn) == []


@pytest.fixture()
def client(tmp_path, monkeypatch):
    config = AppConfig(
        machine="test-machine", db_path=tmp_path / "mediavault.db", hash_algo="sha256",
        tmdb_api_key_env="X", video_extensions=[".mkv"], scan_interval_minutes=15,
    )
    with db.connect(config.db_path) as c:
        db.init_db(c)
    web_app.configure(config)
    monkeypatch.setattr(recycle_bin, "scan_trash", lambda top_n=50: {"items": [], "total_bytes": 0, "accurate": True, "access_errors": []})
    return TestClient(web_app.app, base_url="http://127.0.0.1:8420"), config


def _flash(r):
    return urllib.parse.unquote(urllib.parse.urlparse(r.headers["location"]).query.removeprefix("msg="))


def test_route_requires_confirmation(client, tmp_path):
    c, config = client
    with db.connect(config.db_path) as conn:
        paths = _quarantined(conn, tmp_path)
    r = c.post("/recycle-bin/quarantine/purge-all", data={"confirm": "no"}, follow_redirects=False)
    assert "nothing deleted" in _flash(r).lower() and all(p.exists() for p in paths)


def test_route_purges_and_reports(client, tmp_path):
    c, config = client
    with db.connect(config.db_path) as conn:
        paths = _quarantined(conn, tmp_path)
    r = c.post("/recycle-bin/quarantine/purge-all", data={"confirm": "yes"}, follow_redirects=False)
    assert "Permanently deleted 2 quarantined file(s)" in _flash(r)
    assert not any(p.exists() for p in paths)


def test_route_honours_older_than_days(client, tmp_path):
    c, config = client
    with db.connect(config.db_path) as conn:
        paths = _quarantined(conn, tmp_path)
        _age(conn, 60, db.list_quarantine(conn)[0]["id"])
    r = c.post("/recycle-bin/quarantine/purge-all", data={"confirm": "yes", "older_than_days": "30"}, follow_redirects=False)
    assert "deleted 1 quarantined" in _flash(r) and sum(p.exists() for p in paths) == 1


def test_duplicates_section_stays_when_only_quarantine_is_left(client, tmp_path):
    """After bulk-quarantining every duplicate the section used to vanish --
    taking the purge button with it, right where the user is looking."""
    c, config = client
    with db.connect(config.db_path) as conn:
        _quarantined(conn, tmp_path)
    page = c.get("/").text
    dup = page.index('id="duplicates"')
    assert page.index("quarantine-bar", dup) < page.index("No duplicate groups right now", dup)


def test_purge_bar_is_at_the_top_of_duplicates_and_the_recycle_bin(client, tmp_path):
    c, config = client
    with db.connect(config.db_path) as conn:
        _quarantined(conn, tmp_path)
        other = make_root(tmp_path, label="other", role="inbox", mode="watch")
        write_file(other.path, "a.mkv", b"twin")
        write_file(other.path, "b.mkv", b"twin")
        scanner.scan_root(conn, other, hash_algo="sha256")
    page = c.get("/").text
    assert page.count('action="/recycle-bin/quarantine/purge-all"') == 4          # 2 buttons x 2 sections
    dup = page.index('id="dup-root"')
    assert page.index("quarantine-bar", dup) < page.index("dup-hist", dup)         # above the histogram
    assert "nothing waiting to be purged" not in page


def test_bar_says_when_there_is_nothing_to_purge(client):
    c, _ = client
    page = c.get("/").text
    assert "nothing waiting to be purged" in page and "purge-all" not in page
