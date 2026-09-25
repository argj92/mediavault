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
from dataclasses import dataclass
from pathlib import Path

PLACEHOLDER_SUFFIX = ".icloud"


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


def walk_cloud_aware(root: Path):
    """Like os.walk but yields CloudEntry objects, resolving placeholders to
    the real relative path they stand in for, without ever opening/reading
    them (which would trigger a download)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = Path(dirpath) / name
            if is_placeholder(name):
                real_rel = (Path(dirpath) / real_name_for_placeholder(name)).relative_to(root)
                yield CloudEntry(rel_path=str(real_rel), downloaded=False, local_size=None)
            else:
                rel = full.relative_to(root)
                yield CloudEntry(rel_path=str(rel), downloaded=True, local_size=full.stat().st_size)


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
