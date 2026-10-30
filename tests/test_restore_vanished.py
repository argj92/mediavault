"""A file that vanished from a mirror shows up as a "deletion candidate" --
but MediaVault can't know whether that was deliberate. The Sync section must
offer to put the file back (safe) alongside the irreversible delete."""
import pytest
from fastapi.testclient import TestClient

from mediavault import db, recycle_bin, scanner
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
    monkeypatch.setattr(
        recycle_bin, "scan_trash",
        lambda top_n=50: {"items": [], "total_bytes": 0, "accurate": True, "access_errors": []},
    )
    return TestClient(web_app.app, base_url="http://127.0.0.1:8420"), config


def _library_with_a_vanished_mirror_file(tmp_path, config):
    primary = make_root(tmp_path, label="primary", role="primary", mode="watch")
    mirror = make_root(tmp_path, label="backup", role="backup", mode="mirror")
    write_file(primary.path, "Show/E01.mkv", b"episode one")
    gone = write_file(mirror.path, "Show/E01.mkv", b"episode one")
    with db.connect(config.db_path) as conn:
        db.upsert_root(conn, "primary", str(primary.path), "primary", "watch")
        db.upsert_root(conn, "backup", str(mirror.path), "backup", "mirror")
        scanner.scan_root(conn, primary, hash_algo="sha256")
        scanner.scan_root(conn, mirror, hash_algo="sha256")
        gone.unlink()
        scanner.scan_root(conn, mirror, hash_algo="sha256")
    return primary, mirror


def test_sync_section_offers_to_put_the_files_back(client, tmp_path):
    c, config = client
    _library_with_a_vanished_mirror_file(tmp_path, config)

    page = c.get("/").text

    assert "/sync/backup/restore" in page
    assert "Put them back" in page
    assert "gone from <b>backup</b>, still on <b>primary</b>: Show/E01.mkv" in page


def test_restore_route_copies_back_and_leaves_primary_alone(client, tmp_path):
    c, config = client
    primary, mirror = _library_with_a_vanished_mirror_file(tmp_path, config)

    r = c.post("/sync/backup/restore", follow_redirects=False)

    assert r.status_code == 303
    assert (mirror.path / "Show/E01.mkv").read_bytes() == b"episode one"
    assert (primary.path / "Show/E01.mkv").read_bytes() == b"episode one"
    with db.connect(config.db_path) as conn:
        assert db.get_file(conn, "backup", "Show/E01.mkv")["missing"] == 0


def test_a_moved_folder_is_not_offered_for_deletion(client, tmp_path):
    c, config = client
    primary = make_root(tmp_path, label="primary", role="primary", mode="watch")
    mirror = make_root(tmp_path, label="backup", role="backup", mode="mirror")
    on_mirror = write_file(mirror.path, "Show/E01.mkv", b"episode one")
    with db.connect(config.db_path) as conn:
        db.upsert_root(conn, "primary", str(primary.path), "primary", "watch")
        db.upsert_root(conn, "backup", str(mirror.path), "backup", "mirror")
        scanner.scan_root(conn, mirror, hash_algo="sha256")
        on_mirror.unlink()
        scanner.scan_root(conn, mirror, hash_algo="sha256")
        write_file(primary.path, "Show/E01.mkv", b"episode one")
        scanner.scan_root(conn, primary, hash_algo="sha256")

    page = c.get("/").text

    assert "Put them back" not in page
    assert "/sync/backup/restore" not in page
