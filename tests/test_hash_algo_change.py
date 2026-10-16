"""Hashes are only comparable within one algorithm, so a scan under a
different algorithm than the cached hashes must re-hash instead of mixing."""
from mediavault import db, scanner

from .conftest import write_file
from .test_scanner import make_root


def _count_hashing(monkeypatch):
    calls = []
    real = scanner.hash_file

    def counting(path, algo="sha256"):
        calls.append(path.name)
        return real(path, algo=algo)

    monkeypatch.setattr(scanner, "hash_file", counting)
    return calls


def test_unchanged_files_are_not_rehashed_under_the_same_algorithm(conn, tmp_path, monkeypatch):
    root = make_root(tmp_path, label="lib", role="primary", mode="mirror")
    write_file(root.path, "A.mkv", b"aaa")
    scanner.scan_root(conn, root, hash_algo="sha256")
    calls = _count_hashing(monkeypatch)

    scanner.scan_root(conn, root, hash_algo="sha256")

    assert calls == []


def test_changing_algorithm_rehashes_instead_of_mixing(conn, tmp_path, monkeypatch):
    root = make_root(tmp_path, label="lib", role="primary", mode="mirror")
    write_file(root.path, "A.mkv", b"aaa")
    write_file(root.path, "B.mkv", b"bbb")
    scanner.scan_root(conn, root, hash_algo="sha256")
    assert db.get_file(conn, "lib", "A.mkv")["hash"].startswith("sha256:")
    calls = _count_hashing(monkeypatch)

    scanner.scan_root(conn, root, hash_algo="sha1")

    assert sorted(calls) == ["A.mkv", "B.mkv"]
    assert {f["hash"].split(":")[0] for f in db.all_files(conn, root_label="lib")} == {"sha1"}


def test_blake3_without_the_package_counts_as_sha256(conn, tmp_path, monkeypatch):
    """Requesting blake3 when it isn't installed falls back to sha256 -- and
    must therefore keep reusing existing sha256 hashes, not re-hash forever."""
    from mediavault import hashing
    monkeypatch.setattr(hashing, "_HAVE_BLAKE3", False)
    root = make_root(tmp_path, label="lib", role="primary", mode="mirror")
    write_file(root.path, "A.mkv", b"aaa")
    scanner.scan_root(conn, root, hash_algo="blake3")
    calls = _count_hashing(monkeypatch)

    scanner.scan_root(conn, root, hash_algo="blake3")

    assert calls == [] and db.get_file(conn, "lib", "A.mkv")["hash"].startswith("sha256:")
