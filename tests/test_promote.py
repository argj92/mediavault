from mediavault import db, scanner, sync
from .test_scanner import make_root
from .conftest import write_file


def _seed_backup_only_file(conn, tmp_path):
    """A file that exists on a backup/mirror root but was never seen on
    primary -- the exact shape of a Promote candidate."""
    primary = make_root(tmp_path, label="primary", role="primary", mode="mirror")
    backup = make_root(tmp_path, label="backup", role="backup", mode="mirror")
    write_file(backup.path, "Movie.mkv", b"content")
    scanner.scan_root(conn, primary, hash_algo="sha256")
    scanner.scan_root(conn, backup, hash_algo="sha256")
    return primary, backup


def test_backup_only_file_is_a_copy_to_a_candidate(conn, tmp_path):
    primary, backup = _seed_backup_only_file(conn, tmp_path)
    plan = sync.plan_sync(conn, primary, backup)
    assert plan.copy_to_a == ["Movie.mkv"]


def test_ignored_promotion_is_excluded_from_the_plan(conn, tmp_path):
    primary, backup = _seed_backup_only_file(conn, tmp_path)
    plan = sync.plan_sync(conn, primary, backup, ignored_promotions=frozenset({"Movie.mkv"}))
    assert plan.copy_to_a == []


def test_execute_sync_does_not_promote_by_default(conn, tmp_path):
    """The old behavior (copy_to_a auto-applied by 'easy sync') is gone --
    promoting backup-only content into primary now needs apply_promotions=True,
    from the explicit Promote flow, not a plain sync."""
    primary, backup = _seed_backup_only_file(conn, tmp_path)
    plan = sync.plan_sync(conn, primary, backup)
    results = sync.execute_sync(primary, backup, plan)  # apply_promotions defaults to False

    assert results["copied_to_a"] == 0
    assert not (primary.path / "Movie.mkv").exists()


def test_execute_sync_promotes_when_explicitly_requested(conn, tmp_path):
    primary, backup = _seed_backup_only_file(conn, tmp_path)
    plan = sync.plan_sync(conn, primary, backup)
    results = sync.execute_sync(primary, backup, plan, apply_promotions=True)

    assert results["copied_to_a"] == 1
    assert (primary.path / "Movie.mkv").read_bytes() == b"content"


def test_promote_one_copies_a_single_file(conn, tmp_path):
    primary, backup = _seed_backup_only_file(conn, tmp_path)
    sync.promote_one(backup, primary, "Movie.mkv")
    assert (primary.path / "Movie.mkv").read_bytes() == b"content"


def test_promotion_candidates_lists_across_all_backup_roots(conn, tmp_path):
    primary, backup = _seed_backup_only_file(conn, tmp_path)
    other = make_root(tmp_path, label="other-backup", role="backup", mode="mirror")
    write_file(other.path, "Show.mkv", b"show content")
    scanner.scan_root(conn, other, hash_algo="sha256")

    candidates = sync.promotion_candidates(conn)
    by_path = {c["rel_path"]: c["source_label"] for c in candidates}
    assert by_path == {"Movie.mkv": "backup", "Show.mkv": "other-backup"}


def test_promotion_candidates_excludes_ignored(conn, tmp_path):
    primary, backup = _seed_backup_only_file(conn, tmp_path)
    db.ignore_promotion(conn, "Movie.mkv")
    assert sync.promotion_candidates(conn) == []


def test_ignore_promotion_round_trips_through_db(conn):
    db.ignore_promotion(conn, "Private/Movie.mkv")
    assert db.ignored_promotion_paths(conn) == frozenset({"Private/Movie.mkv"})
    rows = db.list_ignored_promotions(conn)
    assert len(rows) == 1

    db.unignore_promotion(conn, rows[0]["id"])
    assert db.ignored_promotion_paths(conn) == frozenset()


def test_ignore_promotion_is_idempotent(conn):
    db.ignore_promotion(conn, "Movie.mkv")
    db.ignore_promotion(conn, "Movie.mkv")  # must not raise (UNIQUE rel_path)
    assert len(db.list_ignored_promotions(conn)) == 1
