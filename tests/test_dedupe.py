from mediavault import dedupe, db, scanner
from .test_scanner import make_root
from .conftest import write_file


def test_find_duplicates_across_roots(conn, tmp_path):
    # role/mode explicit and distinct from a primary+full-mirror-backup pair
    # (see test_expected_backup_pair_is_not_flagged_as_a_duplicate below for
    # that case) -- this is testing the generic "two roots, same content,
    # neither one the other's designed backup" duplicate detection.
    root_a = make_root(tmp_path, label="a", role="primary", mode="watch")
    root_b = make_root(tmp_path, label="b", role="backup", mode="watch")
    write_file(root_a.path, "Movie.mkv", b"same bytes")
    write_file(root_b.path, "Movie.mkv", b"same bytes")
    write_file(root_a.path, "Other.mkv", b"different")

    scanner.scan_root(conn, root_a, hash_algo="sha256")
    scanner.scan_root(conn, root_b, hash_algo="sha256")

    groups = dedupe.find_duplicates(conn)
    assert len(groups) == 1
    assert groups[0]["count"] == 2
    assert dedupe.total_wasted_bytes(groups) == len(b"same bytes")


def test_expected_backup_pair_is_not_flagged_as_a_duplicate(conn, tmp_path):
    """A file correctly present on both primary and its full-mirror backup
    is the sync feature working as intended -- not something to clean up.
    Only a file doubled up within one root, or one that also turns up
    outside the designed primary/mirror set, should still be flagged."""
    primary = make_root(tmp_path, label="primary", role="primary", mode="mirror")
    backup = make_root(tmp_path, label="backup", role="backup", mode="mirror")
    inbox = make_root(tmp_path, label="downloads", role="inbox", mode="watch")

    write_file(primary.path, "Movie.mkv", b"same bytes")
    write_file(backup.path, "Movie.mkv", b"same bytes")
    scanner.scan_root(conn, primary, hash_algo="sha256")
    scanner.scan_root(conn, backup, hash_algo="sha256")

    groups = dedupe.find_duplicates(conn)
    assert groups == []  # the expected primary+backup pair, correctly excluded

    # The same content also showing up in an unrelated inbox root is still
    # worth flagging -- that's not part of the designed backup set.
    write_file(inbox.path, "Stray.mkv", b"same bytes")
    scanner.scan_root(conn, inbox, hash_algo="sha256")

    groups = dedupe.find_duplicates(conn)
    assert len(groups) == 1
    assert groups[0]["count"] == 3

    # A file doubled up within primary itself is a real duplicate even
    # though a backup also correctly mirrors one of the copies.
    write_file(primary.path, "Movie (copy).mkv", b"same bytes")
    scanner.scan_root(conn, primary, hash_algo="sha256")

    groups = dedupe.find_duplicates(conn)
    assert len(groups) == 1
    assert groups[0]["count"] == 4


def test_quarantine_duplicate_moves_and_deindexes(conn, tmp_path):
    root_a = make_root(tmp_path, label="a")
    root_b = make_root(tmp_path, label="b")
    write_file(root_a.path, "Movie.mkv", b"same bytes")
    write_file(root_b.path, "Movie.mkv", b"same bytes")
    scanner.scan_root(conn, root_a, hash_algo="sha256")
    scanner.scan_root(conn, root_b, hash_algo="sha256")

    quarantine_dir = tmp_path / "quarantine"
    dest = dedupe.quarantine_duplicate(conn, quarantine_dir, root_b.path, "b", "Movie.mkv")

    assert dest.exists()
    assert not (root_b.path / "Movie.mkv").exists()
    assert db.get_file(conn, "b", "Movie.mkv") is None
    assert len(db.list_quarantine(conn)) == 1
