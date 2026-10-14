"""Quarantine from the Duplicates section used to 500 ("Internal Server Error")
whenever the file's root wasn't reachable -- e.g. the Network Drive share not
mounted -- leaving nothing changed and no hint why. Reproduced from the
service log: FileNotFoundError out of shutil.move, unhandled.
"""
import shutil
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from mediavault import db, dedupe, recycle_bin, scanner
from mediavault.config import AppConfig
from mediavault.web import app as web_app

from .conftest import write_file
from .test_scanner import make_root


@pytest.fixture()
def client(tmp_path, monkeypatch):
    config = AppConfig(
        machine="test-machine", db_path=tmp_path / "mediavault.db", hash_algo="sha256",
        tmdb_api_key_env="X", video_extensions=[".mkv"], scan_interval_minutes=15,
    )
    with db.connect(config.db_path) as conn:
        db.init_db(conn)
    web_app.configure(config)
    # GET / would otherwise read the real system Trash.
    monkeypatch.setattr(
        recycle_bin, "scan_trash",
        lambda top_n=50: {"items": [], "total_bytes": 0, "accurate": True, "access_errors": []},
    )
    # No `with TestClient(...)`: that would start the real background scanner.
    return TestClient(web_app.app), config


def _seed_duplicate_in_one_root(tmp_path, config, label="lib"):
    """Two identical files in one root: a genuine duplicate (not a
    primary/backup pair, which the Duplicates section deliberately skips)."""
    root = make_root(tmp_path, label=label, role="primary", mode="watch")
    write_file(root.path, "Movie.mkv", b"same bytes")
    write_file(root.path, "Movie copy.mkv", b"same bytes")
    with db.connect(config.db_path) as conn:
        scanner.scan_root(conn, root, hash_algo="sha256")
    return root


def _flash(response):
    location = response.headers["location"]
    return urllib.parse.unquote(urllib.parse.urlparse(location).query.removeprefix("msg=")), location


def test_unmounted_root_reports_a_message_instead_of_a_500(client, tmp_path):
    c, config = client
    root = _seed_duplicate_in_one_root(tmp_path, config)
    shutil.rmtree(root.path)  # the drive "goes away" after the page was rendered

    r = c.post("/duplicates/quarantine", data={"root_label": "lib", "rel_path": "Movie copy.mkv"}, follow_redirects=False)

    assert r.status_code == 303
    message, location = _flash(r)
    assert "isn't reachable" in message
    assert location.endswith("#duplicates")
    with db.connect(config.db_path) as conn:
        assert db.get_file(conn, "lib", "Movie copy.mkv") is not None  # index untouched
        assert db.list_quarantine(conn) == []


def test_file_already_gone_drops_the_stale_row_so_the_dashboard_updates(client, tmp_path):
    c, config = client
    root = _seed_duplicate_in_one_root(tmp_path, config)
    (root.path / "Movie copy.mkv").unlink()  # removed outside mediavault since the last scan

    r = c.post("/duplicates/quarantine", data={"root_label": "lib", "rel_path": "Movie copy.mkv"}, follow_redirects=False)

    assert r.status_code == 303
    assert "already gone" in _flash(r)[0]
    with db.connect(config.db_path) as conn:
        assert db.get_file(conn, "lib", "Movie copy.mkv") is None  # committed, not rolled back
        assert dedupe.find_duplicates(conn) == []


def test_successful_quarantine_moves_the_file_and_the_dashboard_updates(client, tmp_path):
    c, config = client
    root = _seed_duplicate_in_one_root(tmp_path, config)
    with db.connect(config.db_path) as conn:
        assert len(dedupe.find_duplicates(conn)) == 1  # precondition: it's listed

    r = c.post("/duplicates/quarantine", data={"root_label": "lib", "rel_path": "Movie copy.mkv"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Moved 'Movie copy.mkv' to quarantine" in _flash(r)[0]
    assert not (root.path / "Movie copy.mkv").exists()
    assert (root.path / "Movie.mkv").exists()

    page = c.get("/")
    assert page.status_code == 200
    assert "Quarantine this copy" not in page.text  # no longer listed
    with db.connect(config.db_path) as conn:
        assert dedupe.find_duplicates(conn) == []
        assert len(db.list_quarantine(conn)) == 1


def test_a_move_that_dies_partway_leaves_no_truncated_copy_and_the_original_intact(tmp_path, monkeypatch):
    with db.connect(tmp_path / "m.db") as conn:
        db.init_db(conn)
        root = make_root(tmp_path, label="lib")
        write_file(root.path, "Movie.mkv", b"x" * 100)
        write_file(root.path, "Movie copy.mkv", b"x" * 100)
        scanner.scan_root(conn, root, hash_algo="sha256")
        quarantine_dir = tmp_path / "quarantine"

        def dying_move(src, dst):
            # What a cross-filesystem copy that drops mid-way leaves behind.
            with open(dst, "wb") as f:
                f.write(b"x" * 10)
            raise ConnectionResetError(54, "Connection reset by peer")

        monkeypatch.setattr(dedupe.shutil, "move", dying_move)

        with pytest.raises(dedupe.QuarantineError, match="Nothing was changed"):
            dedupe.quarantine_duplicate(conn, quarantine_dir, root.path, "lib", "Movie copy.mkv")

        assert (root.path / "Movie copy.mkv").read_bytes() == b"x" * 100
        assert list(quarantine_dir.rglob("*.mkv")) == []  # truncated partial removed
        assert db.get_file(conn, "lib", "Movie copy.mkv") is not None
        assert db.list_quarantine(conn) == []


def test_unexpected_drive_error_in_a_post_route_becomes_a_message(client, tmp_path, monkeypatch):
    """The safety net: a route with no handling of its own (here Sync now,
    whose rescan raises when a root is unmounted) must not 500."""
    c, config = client
    primary = make_root(tmp_path, label="primary", role="primary", mode="mirror")
    backup = make_root(tmp_path, label="backup", role="backup", mode="mirror")
    with db.connect(config.db_path) as conn:
        db.upsert_root(conn, "primary", str(primary.path), "primary", "mirror")
        db.upsert_root(conn, "backup", str(backup.path), "backup", "mirror")

    def unmounted(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", str(backup.path))

    monkeypatch.setattr(scanner, "scan_root", unmounted)

    r = c.post("/sync/backup", follow_redirects=False)

    assert r.status_code == 303
    message, location = _flash(r)
    assert "unmounted or unreachable" in message
    assert location.endswith("#sync")


def test_unexpected_drive_error_in_a_get_route_shows_a_friendly_page(client, monkeypatch):
    c, _ = client

    def boom(conn):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(dedupe, "find_duplicates", boom)

    r = c.get("/")

    assert r.status_code == 503
    assert "unmounted or unreachable" in r.text
    assert "Back to the dashboard" in r.text
