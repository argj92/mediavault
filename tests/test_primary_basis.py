from mediavault import db, scanner, sync
from .test_scanner import make_root
from .conftest import write_file


def test_no_primary_means_no_sync_status(conn):
    db.upsert_root(conn, "backup", "/tmp/backup", "backup", "mirror")
    assert sync.sync_status_for_all(conn) == []


def test_count_primary_roots(conn):
    assert db.count_primary_roots(conn) == 0
    db.upsert_root(conn, "lib", "/tmp/lib", "primary", "mirror")
    assert db.count_primary_roots(conn) == 1
    assert db.count_primary_roots(conn, exclude_label="lib") == 0


def test_syncable_roots_excludes_primary_and_watch_mode(conn):
    db.upsert_root(conn, "primary", "/tmp/a", "primary", "mirror")
    db.upsert_root(conn, "backup", "/tmp/b", "backup", "mirror")
    db.upsert_root(conn, "icloud", "/tmp/c", "icloud", "subset")
    db.upsert_root(conn, "downloads", "/tmp/d", "inbox", "watch")

    labels = {r["label"] for r in db.syncable_roots(conn, exclude_label="primary")}
    assert labels == {"backup", "icloud"}


def test_sync_status_for_all_uses_single_primary_as_basis(conn, tmp_path):
    primary = make_root(tmp_path, label="primary", role="primary", mode="mirror")
    backup1 = make_root(tmp_path, label="backup1", role="backup", mode="mirror")
    backup2 = make_root(tmp_path, label="backup2", role="backup", mode="mirror")

    write_file(primary.path, "Movie.mkv", b"content")
    scanner.scan_root(conn, primary, hash_algo="sha256")
    scanner.scan_root(conn, backup1, hash_algo="sha256")
    scanner.scan_root(conn, backup2, hash_algo="sha256")

    statuses = sync.sync_status_for_all(conn)
    by_label = {s["root"].label: s["plan"] for s in statuses}

    assert set(by_label) == {"backup1", "backup2"}
    # No pairs to wire up manually — both backups compare against primary automatically.
    assert by_label["backup1"].copy_to_b == ["Movie.mkv"]
    assert by_label["backup2"].copy_to_b == ["Movie.mkv"]
