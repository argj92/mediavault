from mediavault import db, scanner
from mediavault.config import RootConfig

from .conftest import write_file


def make_root(tmp_path, label="root", role="primary", mode="mirror") -> RootConfig:
    root_dir = tmp_path / label
    root_dir.mkdir()
    return RootConfig(label=label, path=root_dir, role=role, mode=mode)


def test_scan_finds_new_files(conn, tmp_path):
    root = make_root(tmp_path)
    write_file(root.path, "Movie.mkv", b"content")

    stats = scanner.scan_root(conn, root, hash_algo="sha256")

    assert stats["new_files"] == 1
    assert stats["files_scanned"] == 1
    row = db.get_file(conn, root.label, "Movie.mkv")
    assert row["hash"] is not None
    assert row["missing"] == 0


def test_scan_reuses_hash_when_unchanged(conn, tmp_path, monkeypatch):
    root = make_root(tmp_path)
    write_file(root.path, "Movie.mkv", b"content")
    scanner.scan_root(conn, root, hash_algo="sha256")

    calls = []
    from mediavault import scanner as scanner_mod

    original = scanner_mod.hash_file

    def spy(path, algo):
        calls.append(path)
        return original(path, algo)

    monkeypatch.setattr(scanner_mod, "hash_file", spy)
    scanner.scan_root(conn, root, hash_algo="sha256")

    assert calls == []  # unchanged file's cached hash was reused, not recomputed


def test_scan_marks_deleted_file_missing(conn, tmp_path):
    root = make_root(tmp_path)
    f = write_file(root.path, "Movie.mkv", b"content")
    scanner.scan_root(conn, root, hash_algo="sha256")

    f.unlink()
    stats = scanner.scan_root(conn, root, hash_algo="sha256")

    assert stats["missing_files"] == 1
    row = db.get_file(conn, root.label, "Movie.mkv")
    assert row["missing"] == 1


def test_scan_skips_hashing_cloud_placeholder(conn, tmp_path):
    root = make_root(tmp_path, role="icloud", mode="subset")
    write_file(root.path, ".NotHere.mkv.icloud", b"stub")

    stats = scanner.scan_root(conn, root, hash_algo="sha256")

    assert stats["placeholders"] == 1
    row = db.get_file(conn, root.label, "NotHere.mkv")
    assert row["is_placeholder"] == 1
    assert row["hash"] is None
    assert row["missing"] == 0
