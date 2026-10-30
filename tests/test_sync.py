from mediavault import scanner, sync
from .test_scanner import make_root
from .conftest import write_file


def test_plan_copies_new_file_to_missing_side(conn, tmp_path):
    root_a = make_root(tmp_path, label="a")
    root_b = make_root(tmp_path, label="b")
    write_file(root_a.path, "Movie.mkv", b"content")
    scanner.scan_root(conn, root_a, hash_algo="sha256")
    scanner.scan_root(conn, root_b, hash_algo="sha256")

    plan = sync.plan_sync(conn, root_a, root_b)
    assert plan.copy_to_b == ["Movie.mkv"]
    assert plan.copy_to_a == []
    assert plan.out_of_sync is True


def test_execute_sync_copies_files(conn, tmp_path):
    root_a = make_root(tmp_path, label="a")
    root_b = make_root(tmp_path, label="b")
    write_file(root_a.path, "Movie.mkv", b"content")
    scanner.scan_root(conn, root_a, hash_algo="sha256")
    scanner.scan_root(conn, root_b, hash_algo="sha256")

    plan = sync.plan_sync(conn, root_a, root_b)
    results = sync.execute_sync(root_a, root_b, plan)

    assert results["copied_to_b"] == 1
    assert (root_b.path / "Movie.mkv").read_bytes() == b"content"


def test_deletion_from_mirror_root_is_flagged_not_applied(conn, tmp_path):
    root_a = make_root(tmp_path, label="a", mode="mirror")
    root_b = make_root(tmp_path, label="b", mode="mirror")
    f = write_file(root_a.path, "Movie.mkv", b"content")
    write_file(root_b.path, "Movie.mkv", b"content")
    scanner.scan_root(conn, root_a, hash_algo="sha256")
    scanner.scan_root(conn, root_b, hash_algo="sha256")
    scanner.scan_root(conn, root_a, hash_algo="sha256")   # seen on both sides, one after the other

    f.unlink()
    scanner.scan_root(conn, root_a, hash_algo="sha256")

    plan = sync.plan_sync(conn, root_a, root_b)
    assert plan.delete_from_b == ["Movie.mkv"]
    assert plan.copy_to_a == []  # never re-copy just because it's "missing"

    # execute_sync without apply_deletes must leave B's file alone
    sync.execute_sync(root_a, root_b, plan, apply_deletes=False)
    assert (root_b.path / "Movie.mkv").exists()


def test_subset_root_never_generates_deletions(conn, tmp_path):
    """A subset root (e.g. iCloud) legitimately only has some files. A file
    disappearing from it must never be treated as 'delete this from the
    mirror side too'."""
    root_a = make_root(tmp_path, label="a", mode="mirror")
    root_b = make_root(tmp_path, label="b", role="icloud", mode="subset")
    f = write_file(root_b.path, "Movie.mkv", b"content")
    write_file(root_a.path, "Movie.mkv", b"content")
    scanner.scan_root(conn, root_a, hash_algo="sha256")
    scanner.scan_root(conn, root_b, hash_algo="sha256")

    f.unlink()  # e.g. evicted from the subset root
    scanner.scan_root(conn, root_b, hash_algo="sha256")

    plan = sync.plan_sync(conn, root_a, root_b)
    assert plan.delete_from_a == []
    assert plan.delete_from_b == []
    assert plan.copy_to_b == []  # subset root not force-filled with everything either


def test_conflict_detected_for_differing_content(conn, tmp_path):
    root_a = make_root(tmp_path, label="a")
    root_b = make_root(tmp_path, label="b")
    write_file(root_a.path, "Movie.mkv", b"version one")
    write_file(root_b.path, "Movie.mkv", b"version two")
    scanner.scan_root(conn, root_a, hash_algo="sha256")
    scanner.scan_root(conn, root_b, hash_algo="sha256")

    plan = sync.plan_sync(conn, root_a, root_b)
    assert plan.conflicts == ["Movie.mkv"]
    assert plan.needs_confirmation is True


def test_placeholder_counts_as_unverifiable_not_a_diff(conn, tmp_path):
    root_a = make_root(tmp_path, label="a", mode="mirror")
    root_b = make_root(tmp_path, label="b", role="icloud", mode="subset")
    write_file(root_a.path, "Movie.mkv", b"content")
    write_file(root_b.path, ".Movie.mkv.icloud", b"stub")
    scanner.scan_root(conn, root_a, hash_algo="sha256")
    scanner.scan_root(conn, root_b, hash_algo="sha256")

    plan = sync.plan_sync(conn, root_a, root_b)
    assert plan.unverifiable == ["Movie.mkv"]
    assert plan.conflicts == []
    assert plan.copy_to_b == []


def test_restore_vanished_copies_back_instead_of_deleting(conn, tmp_path):
    primary = make_root(tmp_path, label="a", mode="mirror")
    mirror = make_root(tmp_path, label="b", mode="mirror")
    write_file(primary.path, "Show/E01.mkv", b"content")
    gone = write_file(mirror.path, "Show/E01.mkv", b"content")
    scanner.scan_root(conn, primary, hash_algo="sha256")
    scanner.scan_root(conn, mirror, hash_algo="sha256")
    gone.unlink()
    scanner.scan_root(conn, mirror, hash_algo="sha256")

    plan = sync.plan_sync(conn, primary, mirror)
    assert plan.delete_from_a == ["Show/E01.mkv"]

    results = sync.execute_sync(primary, mirror, plan, restore_vanished=True)

    assert results["restored_to_b"] == 1 and results["deleted_from_a"] == 0
    assert (primary.path / "Show/E01.mkv").read_bytes() == b"content"      # primary untouched
    assert (mirror.path / "Show/E01.mkv").read_bytes() == b"content"       # and put back
    scanner.scan_root(conn, mirror, hash_algo="sha256")
    assert not sync.plan_sync(conn, primary, mirror).out_of_sync


def test_plain_sync_never_restores_or_deletes_vanished_files(conn, tmp_path):
    primary = make_root(tmp_path, label="a", mode="mirror")
    mirror = make_root(tmp_path, label="b", mode="mirror")
    write_file(primary.path, "E01.mkv", b"content")
    gone = write_file(mirror.path, "E01.mkv", b"content")
    scanner.scan_root(conn, primary, hash_algo="sha256")
    scanner.scan_root(conn, mirror, hash_algo="sha256")
    gone.unlink()
    scanner.scan_root(conn, mirror, hash_algo="sha256")

    sync.execute_sync(primary, mirror, sync.plan_sync(conn, primary, mirror))

    assert (primary.path / "E01.mkv").exists() and not (mirror.path / "E01.mkv").exists()


def test_a_file_that_was_moved_between_sides_is_a_copy_not_a_deletion(conn, tmp_path):
    """The Burn Notice case: the mirror held the folder for a while, it was
    removed, and only afterwards did the same files show up on primary. They
    were never on both sides at once, so nothing was "deleted from the mirror"
    -- offering to delete primary's only copy would be wrong."""
    primary = make_root(tmp_path, label="a", mode="mirror")
    mirror = make_root(tmp_path, label="b", mode="mirror")
    on_mirror = write_file(mirror.path, "Show/E01.mkv", b"episode")
    scanner.scan_root(conn, mirror, hash_algo="sha256")
    on_mirror.unlink()
    scanner.scan_root(conn, mirror, hash_algo="sha256")          # removed from the mirror
    write_file(primary.path, "Show/E01.mkv", b"episode")          # ...turns up on primary
    scanner.scan_root(conn, primary, hash_algo="sha256")

    plan = sync.plan_sync(conn, primary, mirror)

    assert plan.delete_from_a == [] and plan.delete_from_b == []
    assert plan.copy_to_b == ["Show/E01.mkv"]
    assert not plan.needs_confirmation


def test_a_real_deletion_carries_what_was_seen_and_when(conn, tmp_path):
    primary = make_root(tmp_path, label="a", mode="mirror")
    mirror = make_root(tmp_path, label="b", mode="mirror")
    write_file(primary.path, "E01.mkv", b"episode")
    gone = write_file(mirror.path, "E01.mkv", b"episode")
    scanner.scan_root(conn, primary, hash_algo="sha256")
    scanner.scan_root(conn, mirror, hash_algo="sha256")
    gone.unlink()
    scanner.scan_root(conn, mirror, hash_algo="sha256")

    plan = sync.plan_sync(conn, primary, mirror)

    assert plan.delete_from_a == ["E01.mkv"]
    h = plan.history["E01.mkv"]
    assert h["present_first_seen"] <= h["gone_last_seen"]
