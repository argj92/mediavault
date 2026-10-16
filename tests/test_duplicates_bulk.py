"""Duplicates: keeper/extra selection, the log-scale size histogram, and bulk
quarantine ("keep one, remove the rest" / "remove every copy") for the groups
the section is showing."""
import shutil
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from mediavault import db, dedupe, recycle_bin, scanner
from mediavault.config import AppConfig
from mediavault.web import app as web_app

from .conftest import write_file
from .test_scanner import make_root


def _setup(conn, tmp_path):
    """primary + full mirror backup + an unrelated inbox root, same movie in
    all three (plus a second copy inside primary)."""
    primary = make_root(tmp_path, label="primary", role="primary", mode="mirror")
    backup = make_root(tmp_path, label="backup", role="backup", mode="mirror")
    inbox = make_root(tmp_path, label="downloads", role="inbox", mode="watch")
    write_file(primary.path, "Movie.mkv", b"same bytes")
    write_file(primary.path, "Elsewhere/Movie copy.mkv", b"same bytes")
    write_file(backup.path, "Movie.mkv", b"same bytes")
    write_file(inbox.path, "Stray.mkv", b"same bytes")
    for r in (primary, backup, inbox):
        scanner.scan_root(conn, r, hash_algo="sha256")
    return primary, backup, inbox


def test_keepers_are_one_per_designed_root_and_the_tidiest_path(conn, tmp_path):
    _setup(conn, tmp_path)
    (g,) = dedupe.find_duplicates(conn)

    keep = {(f["root_label"], f["rel_path"]) for f in g["keepers"]}
    extra = {(f["root_label"], f["rel_path"]) for f in g["extras"]}

    assert keep == {("primary", "Movie.mkv"), ("backup", "Movie.mkv")}
    assert extra == {("primary", "Elsewhere/Movie copy.mkv"), ("downloads", "Stray.mkv")}
    # Reclaimable counts only what removing the extras frees -- not every
    # copy but one, since the backup's copy is meant to be there.
    assert g["wasted_bytes"] == 2 * len(b"same bytes")


def test_the_copy_whose_path_matches_the_other_drive_is_kept(conn, tmp_path):
    """Primary has the file at the root and under Games/; the backup has it
    under Games/. The Games/ copy lines up across drives, so the odd-one-out
    at the root is the extra -- even though its path is shorter."""
    primary = make_root(tmp_path, label="primary", role="primary", mode="mirror")
    backup = make_root(tmp_path, label="backup", role="backup", mode="mirror")
    write_file(primary.path, "Clip.mp4", b"clip bytes")
    write_file(primary.path, "Games/Clip.mp4", b"clip bytes")
    write_file(backup.path, "Games/Clip.mp4", b"clip bytes")
    scanner.scan_root(conn, primary, hash_algo="sha256")
    scanner.scan_root(conn, backup, hash_algo="sha256")

    (g,) = dedupe.find_duplicates(conn)

    assert {(f["root_label"], f["rel_path"]) for f in g["keepers"]} == {("primary", "Games/Clip.mp4"), ("backup", "Games/Clip.mp4")}
    assert [(f["root_label"], f["rel_path"]) for f in g["extras"]] == [("primary", "Clip.mp4")]


def test_without_any_matching_paths_the_shortest_path_still_wins(conn, tmp_path):
    root = make_root(tmp_path, label="lib", role="primary", mode="mirror")
    write_file(root.path, "A.mkv", b"same")
    write_file(root.path, "Deeper/Folder/A copy.mkv", b"same")
    scanner.scan_root(conn, root, hash_algo="sha256")
    (g,) = dedupe.find_duplicates(conn)
    assert [f["rel_path"] for f in g["keepers"]] == ["A.mkv"]


def test_content_outside_every_designed_root_keeps_one_copy_overall(conn, tmp_path):
    a = make_root(tmp_path, label="a", role="inbox", mode="watch")
    b = make_root(tmp_path, label="b", role="inbox", mode="watch")
    write_file(a.path, "X.mkv", b"zzz")
    write_file(b.path, "X.mkv", b"zzz")
    scanner.scan_root(conn, a, hash_algo="sha256")
    scanner.scan_root(conn, b, hash_algo="sha256")

    (g,) = dedupe.find_duplicates(conn)

    assert len(g["keepers"]) == 1 and len(g["extras"]) == 1


def test_histogram_buckets_by_per_copy_size_on_a_log_scale(conn, tmp_path):
    r = make_root(tmp_path, label="lib", role="inbox", mode="watch")
    for name, size in {"tiny": 2_100, "small": 40_000, "movie": 3_000_000}.items():
        write_file(r.path, f"{name}1.bin", b"x" * size)
        write_file(r.path, f"{name}2.bin", b"x" * size)
    # distinct contents per pair: perturb one byte per size
    write_file(r.path, "small2.bin", b"y" + b"x" * 39_999)
    write_file(r.path, "small1.bin", b"y" + b"x" * 39_999)
    scanner.scan_root(conn, r, hash_algo="sha256")

    hist = dedupe.size_histogram(dedupe.find_duplicates(conn))

    labels = {h["label"]: h for h in hist}
    assert len(hist) == len(dedupe.SIZE_BUCKETS)
    assert labels["1–4 KB"]["groups"] == 1  # the ~2.1 KB pair
    assert labels["16–64 KB"]["groups"] == 1
    assert labels["1–4 MB"]["groups"] == 1
    assert labels["< 1 KB"]["groups"] == 0 and labels["< 1 KB"]["pct"] == 0
    assert all(h["pct"] >= 3 for h in hist if h["groups"])  # every non-empty bucket is visible


@pytest.mark.parametrize("size,label", [
    (0, "< 1 KB"), (1023, "< 1 KB"), (1024, "1–4 KB"), (4 * 1024 - 1, "1–4 KB"),
    (4 * 1024, "4–16 KB"), (1024**2, "1–4 MB"), (10 * 1024**3, "4 GB+"),
])
def test_bucket_edges(size, label):
    assert dedupe.SIZE_BUCKETS[dedupe.size_bucket_index(size)][0] == label


def test_bulk_extras_keeps_one_per_designed_root(conn, tmp_path):
    primary, backup, inbox = _setup(conn, tmp_path)
    (g,) = dedupe.find_duplicates(conn)

    res = dedupe.quarantine_groups(conn, tmp_path / "q", [g["hash"]], "extras")

    assert res["quarantined"] == 2 and res["groups"] == 1 and res["skipped"] == []
    assert (primary.path / "Movie.mkv").exists() and (backup.path / "Movie.mkv").exists()
    assert not (primary.path / "Elsewhere/Movie copy.mkv").exists()
    assert not (inbox.path / "Stray.mkv").exists()
    assert dedupe.find_duplicates(conn) == []  # nothing left to flag


def test_bulk_all_removes_every_copy(conn, tmp_path):
    primary, backup, inbox = _setup(conn, tmp_path)
    (g,) = dedupe.find_duplicates(conn)

    res = dedupe.quarantine_groups(conn, tmp_path / "q", [g["hash"]], "all")

    assert res["quarantined"] == 4
    assert not list(primary.path.rglob("*.mkv")) and not list(backup.path.rglob("*.mkv")) and not list(inbox.path.rglob("*.mkv"))
    assert len(db.list_quarantine(conn)) == 4  # all recoverable


def test_bulk_only_acts_on_groups_the_section_actually_shows(conn, tmp_path):
    """A stale page or a hand-built request must not be able to quarantine an
    expected primary/backup pair the UI never offers."""
    primary = make_root(tmp_path, label="primary", role="primary", mode="mirror")
    backup = make_root(tmp_path, label="backup", role="backup", mode="mirror")
    write_file(primary.path, "Keep.mkv", b"pair content")
    write_file(backup.path, "Keep.mkv", b"pair content")
    scanner.scan_root(conn, primary, hash_algo="sha256")
    scanner.scan_root(conn, backup, hash_algo="sha256")
    pair_hash = db.get_file(conn, "primary", "Keep.mkv")["hash"]

    res = dedupe.quarantine_groups(conn, tmp_path / "q", [pair_hash, "sha256:not-a-real-hash"], "all")

    assert res["quarantined"] == 0 and res["ignored"] == 2
    assert (primary.path / "Keep.mkv").exists() and (backup.path / "Keep.mkv").exists()


def test_bulk_reports_an_unreachable_drive_and_still_does_the_rest(conn, tmp_path):
    primary, backup, inbox = _setup(conn, tmp_path)
    (g,) = dedupe.find_duplicates(conn)
    shutil.rmtree(backup.path)  # share drops

    res = dedupe.quarantine_groups(conn, tmp_path / "q", [g["hash"]], "all")

    assert res["quarantined"] == 3
    assert len(res["skipped"]) == 1 and "backup" in res["skipped"][0] and "isn't reachable" in res["skipped"][0]
    assert db.get_file(conn, "backup", "Movie.mkv") is not None  # untouched, still indexed


def test_unknown_mode_is_rejected(conn, tmp_path):
    with pytest.raises(ValueError):
        dedupe.quarantine_groups(conn, tmp_path / "q", [], "everything")


# --------------------------------------------------------------- web layer

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


def _flash(r):
    return urllib.parse.unquote(urllib.parse.urlparse(r.headers["location"]).query.removeprefix("msg="))


def test_page_renders_histogram_and_collapsible_groups(client, tmp_path):
    c, config = client
    with db.connect(config.db_path) as conn:
        _setup(conn, tmp_path)

    page = c.get("/").text

    assert 'class="dup-hist"' in page
    assert page.count('<button type="button" class="dup-bar') == len(dedupe.SIZE_BUCKETS)
    assert '<details class="dup-group"' in page            # collapsible, not an open table
    assert "Quarantine all 4 copies" in page
    assert "Quarantine the 2 extra copies" in page


def test_bulk_route_requires_confirmation_and_reports(client, tmp_path):
    c, config = client
    with db.connect(config.db_path) as conn:
        primary, backup, inbox = _setup(conn, tmp_path)
        (g,) = dedupe.find_duplicates(conn)

    r = c.post("/duplicates/quarantine-bulk", data={"hashes": g["hash"], "mode": "extras", "confirm": "no"}, follow_redirects=False)
    assert "nothing changed" in _flash(r).lower()
    assert (inbox.path / "Stray.mkv").exists()

    r = c.post("/duplicates/quarantine-bulk", data={"hashes": g["hash"], "mode": "extras", "confirm": "yes"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Quarantined 2 extra copies across 1 group(s)" in _flash(r)
    assert not (inbox.path / "Stray.mkv").exists()


def test_copies_on_an_offline_drive_cant_be_quarantined_from_the_page(client, tmp_path):
    """The click used to fail with a message the user never saw; say so up front."""
    c, config = client
    with db.connect(config.db_path) as conn:
        primary, backup, inbox = _setup(conn, tmp_path)
    shutil.rmtree(backup.path)

    page = c.get("/").text

    assert "isn't reachable right now" in page and 'class="pill warn">offline' in page
    # only the offline drive's button is disabled
    assert page.count("Quarantine</button>") == 4 and page.count('disabled title="backup') == 1


def test_flash_message_is_dismissible_and_rendered_by_the_shell(client):
    c, _ = client
    page = c.get("/?msg=Quarantined%20it").text
    assert 'class="flash"' in page and "Quarantined it" in page and "flash-close" in page
