from mediavault import cloud


def test_placeholder_detection():
    assert cloud.is_placeholder(".Movie Name.mkv.icloud")
    assert not cloud.is_placeholder("Movie Name.mkv")
    assert not cloud.is_placeholder(".hidden_but_not_icloud")


def test_real_name_for_placeholder():
    assert cloud.real_name_for_placeholder(".Movie Name.mkv.icloud") == "Movie Name.mkv"


def test_walk_cloud_aware_resolves_placeholder(tmp_path):
    (tmp_path / "Downloaded.mkv").write_bytes(b"hello")
    (tmp_path / ".NotDownloaded.mkv.icloud").write_bytes(b"stub")

    entries = {e.rel_path: e for e in cloud.walk_cloud_aware(tmp_path)}
    assert entries["Downloaded.mkv"].downloaded is True
    assert entries["Downloaded.mkv"].local_size == 5
    assert entries["NotDownloaded.mkv"].downloaded is False
    assert entries["NotDownloaded.mkv"].local_size is None


def test_largest_downloaded_files_excludes_placeholders(tmp_path):
    (tmp_path / "big.mkv").write_bytes(b"x" * 5000)
    (tmp_path / "small.mkv").write_bytes(b"x" * 100)
    (tmp_path / ".cloud_only.mkv.icloud").write_bytes(b"stub")

    largest = cloud.largest_downloaded_files(tmp_path, top_n=10)
    names = [rel for rel, _ in largest]
    assert names == ["big.mkv", "small.mkv"]


def test_storage_summary(tmp_path):
    (tmp_path / "a.mkv").write_bytes(b"x" * 1000)
    (tmp_path / ".b.mkv.icloud").write_bytes(b"stub")

    summary = cloud.storage_summary(tmp_path)
    assert summary["downloaded_count"] == 1
    assert summary["placeholder_count"] == 1
    assert summary["downloaded_bytes"] == 1000


def test_walk_cloud_aware_rel_paths_use_forward_slashes(tmp_path):
    """rel_path is stored in the DB and parsed into path components elsewhere
    (e.g. browse.py's folder tree / hidden-folder rules) assuming '/'
    separators. walk_cloud_aware must use .as_posix(), not str(), when
    turning a relative_to() result into a string -- str() of a WindowsPath
    uses backslashes, which would silently break that parsing on Windows.
    This can't be exercised end-to-end on macOS/Linux (str() and .as_posix()
    agree on POSIX), so it's pinned directly against the class actually
    responsible for the platform difference."""
    from pathlib import PureWindowsPath

    windows_style = PureWindowsPath("Movies", "Action", ".hidden", "Secret.mkv")
    assert windows_style.as_posix() == "Movies/Action/.hidden/Secret.mkv"
    assert str(windows_style) == "Movies\\Action\\.hidden\\Secret.mkv"  # what str() would have produced

    (tmp_path / "nested" / "dir").mkdir(parents=True)
    (tmp_path / "nested" / "dir" / "file.mkv").write_bytes(b"x")
    entries = {e.rel_path: e for e in cloud.walk_cloud_aware(tmp_path)}
    assert "nested/dir/file.mkv" in entries
    assert "\\" not in list(entries)[0]
