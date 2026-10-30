"""iCloud: its copies are deliberate (never the 'extra' one), and the space
section answers 'what is using my iCloud, and is it safe to delete?'."""
import subprocess
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from mediavault import db, dedupe, icloud, recycle_bin, scanner
from mediavault.config import AppConfig
from mediavault.web import app as web_app

from .conftest import write_file
from .test_scanner import make_root


# ------------------------------------------------------- keepers in duplicates

def _three_drives(conn, tmp_path, icloud_files, other_files):
    primary = make_root(tmp_path, label="Passport", role="primary", mode="mirror")
    backup = make_root(tmp_path, label="Network", role="backup", mode="mirror")
    cloud = make_root(tmp_path, label="iCloud", role="icloud", mode="subset")
    inbox = make_root(tmp_path, label="Downloads", role="inbox", mode="watch")
    roots = {"Passport": primary, "Network": backup, "iCloud": cloud, "Downloads": inbox}
    for label, rel in other_files + icloud_files:
        write_file(roots[label].path, rel, b"same bytes")
    for r in roots.values():
        scanner.scan_root(conn, r, hash_algo="sha256")
    return roots


def test_the_icloud_copy_is_a_keeper_not_the_extra(conn, tmp_path):
    """The screenshot case: Network + Passport + iCloud all hold the file and
    iCloud was offered up for quarantine."""
    _three_drives(conn, tmp_path, [("iCloud", "Games/G.mkv")], [("Passport", "Games/G.mkv"), ("Network", "Games/G.mkv"), ("Downloads", "G.mkv")])
    (g,) = dedupe.find_duplicates(conn)
    assert {f["root_label"] for f in g["keepers"]} == {"Passport", "Network", "iCloud"}
    assert [f["root_label"] for f in g["extras"]] == ["Downloads"]          # only the stray download is extra


def test_a_file_on_primary_backup_and_icloud_is_not_a_duplicate_at_all(conn, tmp_path):
    _three_drives(conn, tmp_path, [("iCloud", "G.mkv")], [("Passport", "G.mkv"), ("Network", "G.mkv")])
    assert dedupe.find_duplicates(conn) == []


def test_a_second_copy_inside_icloud_is_still_an_extra(conn, tmp_path):
    _three_drives(conn, tmp_path, [("iCloud", "G.mkv"), ("iCloud", "Old/G copy.mkv")], [("Passport", "G.mkv")])
    (g,) = dedupe.find_duplicates(conn)
    assert [(f["root_label"], f["rel_path"]) for f in g["extras"]] == [("iCloud", "Old/G copy.mkv")]


# ------------------------------------------------------------------ analysis

def test_folder_tree_credits_every_folder_on_the_way_down():
    tree = icloud.build_folder_tree([
        ("Movies/DC/Batman.mkv", 4000, 0), ("Movies/DC/Joker.mkv", 3000, 1), ("Movies/Gone Girl.mkv", 1000, 0), ("Top.mkv", 10, 0),
    ])
    assert tree.bytes == 8010 and tree.files == 4
    movies = tree.children["Movies"]
    assert movies.bytes == 8000 and movies.local_bytes == 5000                 # Joker is cloud-only
    assert [c.name for c in movies.sorted_children()] == ["DC", ] and movies.children["DC"].bytes == 7000


def _index(conn, label, rows):
    for rel, size, placeholder, hash_ in rows:
        db.upsert_file(conn, label, rel, size=size, mtime=1.0, hash_=hash_, is_placeholder=bool(placeholder))


def test_analysis_lists_the_biggest_files_and_where_else_they_live(conn):
    db.upsert_root(conn, "iCloud", "/icl", "icloud", "subset")
    db.upsert_root(conn, "Passport", "/pp", "primary", "mirror")
    db.upsert_root(conn, "Network", "/nn", "backup", "mirror")
    _index(conn, "iCloud", [
        ("Movies/Big.mkv", 9000, 0, "sha256:big"),            # hashed: matched by content
        ("Movies/Cloudy.mkv", 5000, 1, None),                  # never downloaded: name + size
        ("Movies/Lonely.mkv", 3000, 1, None),                  # nowhere else
        ("Movies/.Hidden/Secret.mkv", 99999, 0, "sha256:s"),   # hidden folder: counted aside
        ("Tiny.mkv", 1, 0, "sha256:t"),
    ])
    _index(conn, "Passport", [("Films/Big copy.mkv", 9000, 0, "sha256:big"), ("Films/Cloudy.mkv", 5000, 0, "sha256:other")])
    _index(conn, "Network", [("Cloudy.mkv", 5000, 0, "sha256:other"), ("Lonely.mkv", 3001, 0, "sha256:x")])      # wrong size: not a match

    a = icloud.analyze(conn, "iCloud")

    assert [f["name"] for f in a["files"]] == ["Big.mkv", "Cloudy.mkv", "Lonely.mkv", "Tiny.mkv"]       # hidden one excluded
    big, cloudy, lonely = a["files"][:3]
    assert big["elsewhere"] == [{"root": "Passport", "path": "Films/Big copy.mkv", "match": "content"}] and not big["only_copy"]
    assert {(e["root"], e["match"]) for e in cloudy["elsewhere"]} == {("Passport", "name and size"), ("Network", "name and size")}
    assert lonely["only_copy"] and lonely["local"] is False
    assert a["hidden_bytes"] == 99999 and a["total_bytes"] == 9000 + 5000 + 3000 + 1
    assert a["only_copy_bytes"] == 3000 + 1 and a["cloud_only_bytes"] == 8000


def test_files_without_a_size_are_reported_not_hidden(conn):
    db.upsert_root(conn, "iCloud", "/icl", "icloud", "subset")
    _index(conn, "iCloud", [("A.mkv", None, 1, None), ("B.mkv", 10, 0, "sha256:b")])
    a = icloud.analyze(conn, "iCloud")
    assert a["unknown_size"] == 1 and [f["name"] for f in a["files"]] == ["B.mkv"]


def test_path_checks(conn, tmp_path):
    db.upsert_root(conn, "iCloud", str(tmp_path), "icloud", "subset")
    _index(conn, "iCloud", [("Movies/DC/Batman.mkv", 5, 0, "sha256:b")])
    assert icloud.is_known_path(conn, "iCloud", "Movies/DC/Batman.mkv")      # a file
    assert icloud.is_known_path(conn, "iCloud", "Movies")                    # a folder holding files
    assert icloud.is_known_path(conn, "iCloud", "")                          # the root
    assert not icloud.is_known_path(conn, "iCloud", "Movies/Nope") and not icloud.is_known_path(conn, "iCloud", "Mov")
    assert not icloud.is_known_path(conn, "iCloud", "M%")                    # LIKE wildcards are not wildcards here
    assert icloud.resolve_inside(str(tmp_path), "Movies/x") == (tmp_path / "Movies" / "x").resolve()
    assert icloud.resolve_inside(str(tmp_path), "../outside") is None


# --------------------------------------------------------------------- route

def _intercept_open(monkeypatch, handler):
    """Replace only `open ...` calls (the host guard also shells out, to tailscale)."""
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: handler(cmd) if cmd[0] == "open" else real(cmd, **kw))

@pytest.fixture()
def client(tmp_path, monkeypatch):
    config = AppConfig(machine="m", db_path=tmp_path / "mv.db", hash_algo="sha256", tmdb_api_key_env="X",
                       video_extensions=[".mkv"], scan_interval_minutes=15)
    cloud = tmp_path / "icloud"
    (cloud / "Movies").mkdir(parents=True)
    (cloud / "Movies" / "Big.mkv").write_bytes(b"x" * 100)
    (tmp_path / "elsewhere").mkdir()
    with db.connect(config.db_path) as conn:
        db.init_db(conn)
        db.upsert_root(conn, "iCloud", str(cloud), "icloud", "subset")
        db.upsert_root(conn, "Other", str(tmp_path / "elsewhere"), "backup", "mirror")
        _index(conn, "iCloud", [("Movies/Big.mkv", 100, 0, "sha256:b")])
        _index(conn, "Other", [("Secret.mkv", 5, 0, "sha256:s")])
    web_app.configure(config)
    web_app.app.state.worker = None
    monkeypatch.setattr(recycle_bin, "scan_trash", lambda top_n=50: {"items": [], "total_bytes": 0, "accurate": True, "access_errors": []})
    return TestClient(web_app.app, base_url="http://127.0.0.1:8420"), monkeypatch, cloud


def _flash(r):
    return urllib.parse.unquote(urllib.parse.urlparse(r.headers["location"]).query.removeprefix("msg=")).split("#")[0]


def test_reveal_opens_finder_on_a_known_icloud_path_only(client):
    c, mp, cloud = client
    calls = []
    mp.setattr(web_app.platform, "system", lambda: "Darwin")
    _intercept_open(mp, calls.append)

    ok = c.post("/icloud/reveal", data={"root_label": "iCloud", "rel_path": "Movies/Big.mkv"}, follow_redirects=False)
    assert calls == [["open", "-R", str((cloud / "Movies" / "Big.mkv").resolve())]] and "Showing" in _flash(ok)

    for bad in (
        {"root_label": "iCloud", "rel_path": "Movies/Unknown.mkv"},          # not in the index
        {"root_label": "iCloud", "rel_path": "../elsewhere/Secret.mkv"},     # escapes the root
        {"root_label": "Other", "rel_path": "Secret.mkv"},                   # not an iCloud root
        {"root_label": "Nope", "rel_path": "x"},
    ):
        calls.clear()
        r = c.post("/icloud/reveal", data=bad, follow_redirects=False)
        assert calls == [] and "isn't something" in _flash(r), bad


def test_reveal_says_so_when_the_file_has_gone(client):
    c, mp, cloud = client
    (cloud / "Movies" / "Big.mkv").unlink()
    _intercept_open(mp, lambda cmd: pytest.fail("must not run"))
    assert "isn't there any more" in _flash(c.post("/icloud/reveal", data={"root_label": "iCloud", "rel_path": "Movies/Big.mkv"}, follow_redirects=False))


def test_page_shows_folders_files_and_where_else(client):
    c, mp, cloud = client
    page = c.get("/").text
    assert "Biggest folders" in page and "Biggest files" in page and "Movies" in page and "Big.mkv" in page
    assert 'action="/icloud/reveal"' in page and "only copy" in page
