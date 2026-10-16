"""OS metadata clutter that rides along when a library lives on (or is copied
across) non-native filesystems -- macOS Finder view settings (.DS_Store),
AppleDouble "._*" sidecar files created by SMB/exFAT/NTFS volumes, Windows
Thumbs.db/desktop.ini, Spotlight/fseventsd indexes. None of it is media, and
none of it is meaningful to back up, sync, dedupe, or guess a title for.

Left in the index it made things lie: a SMB-mounted backup carried hundreds of
"._*" files the primary didn't have, and every .DS_Store differs between
drives, so the backup showed permanently out of sync with "conflicts" that
were all Finder view settings.

The list is deliberately conservative -- only files/folders an OS generates
for its own bookkeeping. It must NOT catch real user content that merely
starts with a dot, or mediavault's own `.mediavault-hide` marker (which has to
stay in the index for the Library's hidden-folder rule to work). User-deleted
data (.Trashes, $RECYCLE.BIN) is intentionally *not* treated as junk.
"""
from __future__ import annotations

from pathlib import PurePosixPath

JUNK_FILENAMES = frozenset({
    ".DS_Store", ".localized", ".apdisk", ".VolumeIcon.icns",
    "Thumbs.db", "ehthumbs.db", "desktop.ini",
})

JUNK_DIRNAMES = frozenset({
    ".Spotlight-V100", ".fseventsd", ".TemporaryItems", ".DocumentRevisions-V100",
    ".AppleDouble", "System Volume Information",
})

APPLEDOUBLE_PREFIX = "._"


def is_junk_name(name: str) -> bool:
    """A file basename that's OS bookkeeping. `._anything` is an AppleDouble
    sidecar -- including `._Movie.mkv`, which ends in a video extension and
    would otherwise pass for a real video."""
    return name in JUNK_FILENAMES or name.startswith(APPLEDOUBLE_PREFIX)


def is_junk_dirname(name: str) -> bool:
    return name in JUNK_DIRNAMES


def is_junk_path(rel_path: str) -> bool:
    """Whether a root-relative, forward-slash path is OS junk: a junk file, or
    anything inside a junk folder."""
    parts = PurePosixPath(rel_path).parts
    if not parts:
        return False
    return is_junk_name(parts[-1]) or any(is_junk_dirname(p) for p in parts[:-1])
