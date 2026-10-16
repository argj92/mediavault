"""Evicted iCloud files keep their name and size but have no data ("dataless").
Reading one downloads it (interactively) or fails with EDEADLK (background
service). The scanner must neither read them nor give up on the whole root
because of them."""
import errno
import os

import pytest

from mediavault import cloud, db, scanner
from mediavault.config import RootConfig

from .conftest import write_file
from .test_scanner import make_root


@pytest.fixture()
def dataless(monkeypatch):
    """Marks files by name as dataless, as stat()'s SF_DATALESS flag would."""
    names = set()
    monkeypatch.setattr(cloud, "is_dataless", lambda st: st.st_size in names)
    return names


def _forbid_reading(monkeypatch, forbidden_names):
    real = scanner.hash_file

    def hash_file(path, algo="sha256"):
        assert path.name not in forbidden_names, f"read {path.name}, which would download it"
        return real(path, algo=algo)

    monkeypatch.setattr(scanner, "hash_file", hash_file)


def test_is_dataless_reads_the_macos_flag():
    class St: st_flags = cloud.SF_DATALESS | 0x60
    class Local: st_flags = 0x60
    class NoFlags: pass
    assert cloud.is_dataless(St) and not cloud.is_dataless(Local) and not cloud.is_dataless(NoFlags)


def test_walk_marks_dataless_files_not_downloaded_but_keeps_their_size(tmp_path, dataless):
    root = make_root(tmp_path, label="icloud", role="icloud", mode="subset")
    write_file(root.path, "Cloud.mkv", b"x" * 777)
    write_file(root.path, "Local.mkv", b"y" * 10)
    dataless.add(777)

    entries = {e.rel_path: e for e in cloud.walk_cloud_aware(root.path)}

    assert entries["Cloud.mkv"].downloaded is False and entries["Cloud.mkv"].size == 777
    assert entries["Local.mkv"].downloaded is True and entries["Local.mkv"].local_size == 10


def test_scan_indexes_dataless_files_without_reading_them(conn, tmp_path, dataless, monkeypatch):
    root = make_root(tmp_path, label="icloud", role="icloud", mode="subset")
    write_file(root.path, "Cloud.mkv", b"x" * 777)
    write_file(root.path, "Local.mkv", b"y" * 10)
    dataless.add(777)
    _forbid_reading(monkeypatch, {"Cloud.mkv"})

    stats = scanner.scan_root(conn, root, hash_algo="sha256")

    cloud_row = db.get_file(conn, "icloud", "Cloud.mkv")
    assert cloud_row["is_placeholder"] == 1 and cloud_row["hash"] is None and cloud_row["size"] == 777
    assert db.get_file(conn, "icloud", "Local.mkv")["hash"].startswith("sha256:")
    assert stats["placeholders"] == 1 and stats["missing_files"] == 0


def test_a_file_that_was_hashed_then_evicted_keeps_its_hash_and_isnt_marked_missing(conn, tmp_path, dataless, monkeypatch):
    root = make_root(tmp_path, label="icloud", role="icloud", mode="subset")
    write_file(root.path, "Movie.mkv", b"x" * 777)
    scanner.scan_root(conn, root, hash_algo="sha256")
    first_hash = db.get_file(conn, "icloud", "Movie.mkv")["hash"]
    dataless.add(777)                                   # evicted since
    _forbid_reading(monkeypatch, {"Movie.mkv"})

    scanner.scan_root(conn, root, hash_algo="sha256")

    row = db.get_file(conn, "icloud", "Movie.mkv")
    assert row["hash"] == first_hash and row["missing"] == 0


def _fail_hash_with(monkeypatch, name, code):
    real = scanner.hash_file

    def hash_file(path, algo="sha256"):
        if path.name == name:
            raise OSError(code, os.strerror(code))
        return real(path, algo=algo)

    monkeypatch.setattr(scanner, "hash_file", hash_file)


@pytest.mark.parametrize("code", [errno.EDEADLK, errno.EACCES, errno.EPERM])
def test_one_unreadable_file_doesnt_fail_the_whole_root(conn, tmp_path, monkeypatch, code):
    root = make_root(tmp_path, label="lib", role="icloud", mode="subset")
    write_file(root.path, "Bad.mkv", b"bad")
    write_file(root.path, "Good.mkv", b"good")
    _fail_hash_with(monkeypatch, "Bad.mkv", code)

    stats = scanner.scan_root(conn, root, hash_algo="sha256")

    assert stats["unreadable"] == 1
    assert db.get_file(conn, "lib", "Good.mkv")["hash"].startswith("sha256:")
    assert db.get_file(conn, "lib", "Bad.mkv")["is_placeholder"] == 1          # present, just not readable now
    assert db.get_last_finished_scan(conn, "lib") is not None and db.get_abandoned_scan(conn, "lib") is None


@pytest.mark.parametrize("code", [errno.EIO, errno.ENOTCONN, errno.ESTALE])
def test_drive_level_errors_still_abort_the_scan(conn, tmp_path, monkeypatch, code):
    """A dropped share mid-scan must not be papered over file by file."""
    root = make_root(tmp_path, label="lib", role="backup", mode="mirror")
    write_file(root.path, "A.mkv", b"a")
    _fail_hash_with(monkeypatch, "A.mkv", code)
    with pytest.raises(OSError):
        scanner.scan_root(conn, root, hash_algo="sha256")
    assert db.get_abandoned_scan(conn, "lib")["error"]
