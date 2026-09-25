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
