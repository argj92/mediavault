from mediavault import db


def test_update_role_mode_changes_in_place_without_touching_path(conn):
    db.upsert_root(conn, "icloud-drive", "/Volumes/iCloud", "icloud", "subset")
    db.update_root_role_mode(conn, "icloud-drive", "backup", "mirror")

    row = db.get_root(conn, "icloud-drive")
    assert row["role"] == "backup"
    assert row["mode"] == "mirror"
    assert row["path"] == "/Volumes/iCloud"  # untouched


def test_update_preserves_indexed_files_unlike_remove_and_readd(conn):
    """The whole point of edit-in-place: unlike remove()+add(), the file
    index for this root must survive a role/mode change."""
    db.upsert_root(conn, "backup", "/Volumes/Backup", "backup", "mirror")
    db.upsert_file(conn, "backup", "Movie.mkv", 100, 1.0, "sha256:abc", False)

    db.update_root_role_mode(conn, "backup", "icloud", "subset")

    assert db.get_file(conn, "backup", "Movie.mkv") is not None


def test_promoting_a_second_root_to_primary_is_the_caller_s_job_to_block(conn):
    """db.update_root_role_mode itself has no opinion on the single-primary
    rule -- that's enforced by callers (cli.py/web/app.py), same as
    upsert_root. This test documents that split, since it'd be an easy
    place to accidentally regress the constraint."""
    db.upsert_root(conn, "primary", "/Movies", "primary", "mirror")
    db.upsert_root(conn, "backup", "/Backup", "backup", "mirror")

    db.update_root_role_mode(conn, "backup", "primary", "mirror")

    assert db.count_primary_roots(conn) == 2  # unenforced at this layer, by design
