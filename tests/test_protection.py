from mediavault import protection, scanner
from .test_scanner import make_root
from .conftest import write_file


def test_single_copy_is_at_risk(conn, tmp_path):
    primary = make_root(tmp_path, label="primary", role="primary", mode="mirror")
    write_file(primary.path, "Movie.mkv", b"only copy")
    scanner.scan_root(conn, primary, hash_algo="sha256")

    report = protection.compute_protection(conn, "this-machine")

    assert report["total_groups"] == 1
    assert report["covered"] == 0
    assert len(report["at_risk"]) == 1
    assert report["at_risk"][0]["protective_count"] == 1


def test_two_mirror_roots_covers_it(conn, tmp_path):
    primary = make_root(tmp_path, label="primary", role="primary", mode="mirror")
    backup = make_root(tmp_path, label="backup", role="backup", mode="mirror")
    write_file(primary.path, "Movie.mkv", b"safe copy")
    write_file(backup.path, "Movie.mkv", b"safe copy")
    scanner.scan_root(conn, primary, hash_algo="sha256")
    scanner.scan_root(conn, backup, hash_algo="sha256")

    report = protection.compute_protection(conn, "this-machine")

    assert report["covered"] == 1
    assert report["at_risk"] == []


def test_subset_root_never_counts_toward_protection(conn, tmp_path):
    """A file that only exists on the primary and in an iCloud subset copy
    is still at risk -- the subset copy is a convenience, not a backup."""
    primary = make_root(tmp_path, label="primary", role="primary", mode="mirror")
    icloud = make_root(tmp_path, label="icloud", role="icloud", mode="subset")
    write_file(primary.path, "Movie.mkv", b"content")
    write_file(icloud.path, "Movie.mkv", b"content")
    scanner.scan_root(conn, primary, hash_algo="sha256")
    scanner.scan_root(conn, icloud, hash_algo="sha256")

    report = protection.compute_protection(conn, "this-machine")

    assert report["covered"] == 0
    assert report["at_risk"][0]["protective_count"] == 1


def test_at_risk_sorted_by_size_descending(conn, tmp_path):
    primary = make_root(tmp_path, label="primary", role="primary", mode="mirror")
    write_file(primary.path, "Small.mkv", b"x" * 100)
    write_file(primary.path, "Big.mkv", b"y" * 5000)
    scanner.scan_root(conn, primary, hash_algo="sha256")

    report = protection.compute_protection(conn, "this-machine")

    sizes = [g["size"] for g in report["at_risk"]]
    assert sizes == sorted(sizes, reverse=True)
