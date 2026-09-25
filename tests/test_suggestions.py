from mediavault import scanner, suggestions
from .test_scanner import make_root
from .conftest import write_file


def test_untracked_media_flags_new_file_not_in_library(conn, tmp_path):
    library = make_root(tmp_path, label="library", role="primary", mode="mirror")
    inbox = make_root(tmp_path, label="inbox", role="inbox", mode="watch")
    write_file(library.path, "Existing.mkv", b"already have this")
    write_file(inbox.path, "New.mkv", b"brand new")
    write_file(inbox.path, "Existing.mkv", b"already have this")  # dup of library

    scanner.scan_root(conn, library, hash_algo="sha256")
    scanner.scan_root(conn, inbox, hash_algo="sha256")

    results = suggestions.untracked_media(conn, [".mkv"])
    by_path = {r["rel_path"]: r for r in results}

    assert by_path["New.mkv"]["already_in_library"] is False
    assert by_path["Existing.mkv"]["already_in_library"] is True


def test_untracked_media_ignores_non_video_extensions(conn, tmp_path):
    inbox = make_root(tmp_path, label="inbox", role="inbox", mode="watch")
    write_file(inbox.path, "notes.txt", b"hello")
    scanner.scan_root(conn, inbox, hash_algo="sha256")

    results = suggestions.untracked_media(conn, [".mkv"])
    assert results == []
