"""The system-trash view needs macOS Full Disk Access. The page should say
exactly what to grant it to, and offer to open the right settings."""
import sys
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mediavault import db, recycle_bin
from mediavault.config import AppConfig
from mediavault.web import app as web_app


def test_the_app_to_grant_is_the_python_app_bundle_not_the_venv_symlink(tmp_path, monkeypatch):
    real = tmp_path / "Cellar" / "Python.app" / "Contents" / "MacOS" / "Python"
    real.parent.mkdir(parents=True)
    real.write_text("")
    link = tmp_path / "venv" / "bin" / "python"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    monkeypatch.setattr(sys, "executable", str(link))

    assert recycle_bin.app_needing_access() == (tmp_path / "Cellar" / "Python.app").resolve()


def test_without_an_app_bundle_it_is_the_executable_itself(tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "python3.12"
    exe.parent.mkdir()
    exe.write_text("")
    monkeypatch.setattr(sys, "executable", str(exe))
    assert recycle_bin.app_needing_access() == exe.resolve()


def test_opening_settings_uses_a_fixed_url_and_reveal_uses_the_resolved_app(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(recycle_bin.platform, "system", lambda: "Darwin")
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(recycle_bin, "app_needing_access", lambda: tmp_path / "Python.app")

    assert recycle_bin.open_full_disk_access_settings()[0] is True
    assert recycle_bin.reveal_app_needing_access()[0] is True

    assert calls == [["open", recycle_bin.FULL_DISK_ACCESS_PANE], ["open", "-R", str(tmp_path / "Python.app")]]
    assert recycle_bin.FULL_DISK_ACCESS_PANE.endswith("Privacy_AllFiles")


def test_nothing_is_opened_off_macos(monkeypatch):
    monkeypatch.setattr(recycle_bin.platform, "system", lambda: "Linux")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("must not run"))
    ok, msg = recycle_bin.open_full_disk_access_settings()
    assert not ok and "macOS" in msg


@pytest.fixture()
def client(tmp_path, monkeypatch):
    config = AppConfig(machine="m", db_path=tmp_path / "mv.db", hash_algo="sha256", tmdb_api_key_env="X",
                       video_extensions=[".mkv"], scan_interval_minutes=15)
    with db.connect(config.db_path) as conn:
        db.init_db(conn)
    web_app.configure(config)
    web_app.app.state.worker = None
    return TestClient(web_app.app, base_url="http://127.0.0.1:8420"), monkeypatch


def _trash(monkeypatch, accurate, errors=()):
    monkeypatch.setattr(recycle_bin, "scan_trash", lambda top_n=50: {
        "items": [], "total_bytes": 0, "accurate": accurate,
        "access_errors": [recycle_bin.TrashAccessError(Path(p), "[Errno 1] Operation not permitted") for p in errors]})


def test_the_warning_names_the_program_and_offers_the_buttons(client):
    c, mp = client
    _trash(mp, False, ["/Users/x/.Trash"])
    mp.setattr(recycle_bin, "app_needing_access", lambda: Path("/opt/homebrew/Cellar/python@3.12/3.12.14/Python.app"))

    page = c.get("/").text

    assert "isn't letting MediaVault read your Trash" in page and "/Users/x/.Trash" in page
    assert "/opt/homebrew/Cellar/python@3.12/3.12.14/Python.app" in page
    assert 'action="/system/full-disk-access"' in page and 'action="/system/reveal-app"' in page
    assert "don't need it" in page                      # says what is and isn't affected
    assert 'class="notice warn"' in page and "flash warn-flash" not in page   # inline, not the sticky toast


def test_no_warning_when_the_trash_is_readable(client):
    c, mp = client
    _trash(mp, True)
    assert "trash-access" not in c.get("/").text


def test_the_buttons_open_things_on_this_mac_and_flash_the_result(client):
    c, mp = client
    seen = []
    mp.setattr(recycle_bin, "open_full_disk_access_settings", lambda: (seen.append("pane") or True, "x"))
    mp.setattr(recycle_bin, "reveal_app_needing_access", lambda: (seen.append("reveal") or True, "x"))
    r1 = c.post("/system/full-disk-access", follow_redirects=False)
    r2 = c.post("/system/reveal-app", follow_redirects=False)
    assert seen == ["pane", "reveal"]
    assert "Full Disk Access" in urllib.parse.unquote(r1.headers["location"]) and "Finder" in urllib.parse.unquote(r2.headers["location"])
