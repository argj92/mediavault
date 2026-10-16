"""Awareness of cloud-backed folders (iCloud Drive and similar) where files
are not always fully present on disk.

macOS represents a not-yet-downloaded iCloud Drive file as a small placeholder
named ``.<realname>.icloud`` sitting next to where the real file would be.
The real file simply doesn't exist locally yet — reading it forces a download.
We detect these placeholders by name so scanning never accidentally triggers
downloads of the whole library and never mistakes "not downloaded" for
"deleted".
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from . import junk

PLACEHOLDER_SUFFIX = ".icloud"

# Current macOS doesn't leave ".name.icloud" stubs for evicted files: the file
# keeps its real name and size but has no data on disk, flagged "dataless"
# (SF_DATALESS) with zero blocks. Reading it makes macOS download the whole
# file -- from a background service the read instead fails with EDEADLK
# ("Resource deadlock avoided"). stat() alone is safe and never downloads.
SF_DATALESS = getattr(stat, "SF_DATALESS", 0x40000000)


def is_dataless(st) -> bool:
    return bool(getattr(st, "st_flags", 0) & SF_DATALESS)


def is_placeholder(entry_name: str) -> bool:
    return entry_name.startswith(".") and entry_name.endswith(PLACEHOLDER_SUFFIX)


def real_name_for_placeholder(entry_name: str) -> str:
    """.MyMovie.mkv.icloud -> MyMovie.mkv"""
    assert is_placeholder(entry_name)
    return entry_name[1 : -len(PLACEHOLDER_SUFFIX)]


@dataclass
class CloudEntry:
    rel_path: str
    downloaded: bool
    local_size: int | None  # None when it's a placeholder we can't size
    size: int | None = None  # the file's real size when known, even if it isn't downloaded


def walk_cloud_aware(root: Path):
    """Like os.walk but yields CloudEntry objects, resolving placeholders to
    the real relative path they stand in for, without ever opening/reading
    them (which would trigger a download)."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune in place so os.walk never descends into OS-generated folders
        # (Spotlight/fseventsd indexes, ...); and skip OS junk files
        # (.DS_Store, AppleDouble "._*") entirely -- see junk.py for why they
        # must never reach the index.
        dirnames[:] = sorted(d for d in dirnames if not junk.is_junk_dirname(d))
        for name in sorted(filenames):
            if junk.is_junk_name(name):
                continue
            full = Path(dirpath) / name
            if is_placeholder(name):
                real_rel = (Path(dirpath) / real_name_for_placeholder(name)).relative_to(root)
                # .as_posix(), not str(): rel_path is stored in the DB and
                # treated as forward-slash-separated everywhere it's parsed
                # into components (e.g. browse.py's folder tree / hidden-
                # folder rules). str() of a WindowsPath uses backslashes,
                # which silently breaks that parsing on Windows.
                yield CloudEntry(rel_path=real_rel.as_posix(), downloaded=False, local_size=None)
            else:
                rel = full.relative_to(root)
                st = full.stat()
                if is_dataless(st):
                    yield CloudEntry(rel_path=rel.as_posix(), downloaded=False, local_size=None, size=st.st_size)
                else:
                    yield CloudEntry(rel_path=rel.as_posix(), downloaded=True, local_size=st.st_size, size=st.st_size)


def largest_downloaded_files(root: Path, top_n: int = 25) -> list[tuple[str, int]]:
    """Files that ARE fully local right now under a cloud-backed root, largest
    first. These are the safe candidates to evict (e.g. via Finder's "Remove
    Download") to reclaim local disk space — the content stays safe in the
    cloud copy, so this is never something mediavault deletes itself."""
    entries = [
        (e.rel_path, e.local_size)
        for e in walk_cloud_aware(root)
        if e.downloaded and e.local_size is not None
    ]
    entries.sort(key=lambda t: t[1], reverse=True)
    return entries[:top_n]


def storage_summary(root: Path) -> dict:
    downloaded = 0
    placeholder = 0
    downloaded_bytes = 0
    for e in walk_cloud_aware(root):
        if e.downloaded:
            downloaded += 1
            downloaded_bytes += e.local_size or 0
        else:
            placeholder += 1
    return {
        "downloaded_count": downloaded,
        "placeholder_count": placeholder,
        "downloaded_bytes": downloaded_bytes,
    }
