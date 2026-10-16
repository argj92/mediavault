"""OS junk (.DS_Store, AppleDouble "._*", ...) must never reach the index.

Found live: a SMB-mounted backup carried 623 "._*" files the primary didn't
have, and every .DS_Store differed between drives -- so the backup sat
permanently "out of sync" with 4 "conflicts" that were all Finder view
settings, and (once the primary was set to mirror) Promote would have offered
623 junk files "to move to primary" alongside the 5 real ones.
"""
import pytest

from mediavault import cloud, db, junk, scanner, sync
from .conftest import write_file
from .test_scanner import make_root


@pytest.mark.parametrize("path", [
    ".DS_Store", "Movies/.DS_Store", "._Movie.mkv", "Movies/Action/._Movie.mkv",
    "._.mediavault-hide", "Show/Thumbs.db", "desktop.ini", ".localized",
    ".Spotlight-V100/Store-V2/x", "Movies/.fseventsd/uuid", "System Volume Information/x.dat",
])
def test_junk_paths(path):
    assert junk.is_junk_path(path)


@pytest.mark.parametrize("path", [
    "Movie.mkv", "Movies/Action/Movie.mkv",
    ".mediavault-hide",              # mediavault's own marker must stay indexed
    "Private/.mediavault-hide",
    ".hidden-but-real.mkv",           # a dotfile is not automatically junk
    ".Trashes/501/deleted.mkv",       # user-deleted data, not OS bookkeeping
    "Movies/Underscore_in_name.mkv",
    "Movies/_.mkv",
    "notes.._.txt",
])
def test_non_junk_paths(path):
    assert not junk.is_junk_path(path)


def test_walk_skips_junk_files_and_junk_folders(tmp_path):
    write_file(tmp_path, "Movie.mkv", b"a")
    write_file(tmp_path, ".DS_Store", b"x")
    write_file(tmp_path, "._Movie.mkv", b"x")
    write_file(tmp_path, ".mediavault-hide", b"")
    write_file(tmp_path, ".Spotlight-V100/Store/index", b"x")
    write_file(tmp_path, "Sub/._other.mkv", b"x")
    write_file(tmp_path, "Sub/other.mkv", b"b")

    found = {e.rel_path for e in cloud.walk_cloud_aware(tmp_path)}

    assert found == {"Movie.mkv", ".mediavault-hide", "Sub/other.mkv"}


def test_scan_purges_junk_rows_indexed_before_the_rule_existed(conn, tmp_path):
    root = make_root(tmp_path, label="lib")
    write_file(root.path, "Movie.mkv", b"a")
    db.upsert_root(conn, "lib", str(root.path), "primary", "mirror")
    db.upsert_file(conn, "lib", ".DS_Store", 10, 1.0, "sha256:j", False)
    db.upsert_file(conn, "lib", "Sub/._Movie.mkv", 10, 1.0, "sha256:k", False)

    scanner.scan_root(conn, root, hash_algo="sha256")

    paths = {r["rel_path"] for r in db.all_files(conn, root_label="lib", include_missing=True)}
    assert paths == {"Movie.mkv"}  # deleted outright, not left as "missing"


def test_junk_no_longer_makes_a_synced_backup_look_out_of_sync(conn, tmp_path):
    """The live symptom: identical real content, differing Finder junk."""
    primary = make_root(tmp_path, label="primary", role="primary", mode="mirror")
    backup = make_root(tmp_path, label="backup", role="backup", mode="mirror")
    write_file(primary.path, "Movie.mkv", b"same")
    write_file(backup.path, "Movie.mkv", b"same")
    write_file(primary.path, ".DS_Store", b"view settings on the primary")
    write_file(backup.path, ".DS_Store", b"different view settings on the backup")
    write_file(backup.path, "._Movie.mkv", b"smb sidecar")
    write_file(backup.path, "Sub/._Other.mkv", b"smb sidecar")

    scanner.scan_root(conn, primary, hash_algo="sha256")
    scanner.scan_root(conn, backup, hash_algo="sha256")

    plan = sync.plan_sync(conn, primary, backup)
    assert plan.conflicts == []
    assert plan.copy_to_a == [] and plan.copy_to_b == []
    assert plan.out_of_sync is False
    assert sync.promotion_candidates(conn) == []
