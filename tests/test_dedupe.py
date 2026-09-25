from mediavault import dedupe, db, scanner
from .test_scanner import make_root
from .conftest import write_file


def test_find_duplicates_across_roots(conn, tmp_path):
    root_a = make_root(tmp_path, label="a")
    root_b = make_root(tmp_path, label="b")
    write_file(root_a.path, "Movie.mkv", b"same bytes")
    write_file(root_b.path, "Movie.mkv", b"same bytes")
    write_file(root_a.path, "Other.mkv", b"different")

    scanner.scan_root(conn, root_a, hash_algo="sha256")
    scanner.scan_root(conn, root_b, hash_algo="sha256")

    groups = dedupe.find_duplicates(conn)
    assert len(groups) == 1
    assert groups[0]["count"] == 2
    assert dedupe.total_wasted_bytes(groups) == len(b"same bytes")


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
