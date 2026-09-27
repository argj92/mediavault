"""Groups a root's tracked video files into a folder tree for browsing and
playback from the web UI.

Two ways a folder can stay out of the browse tree while remaining fully
scanned/synced/deduped like any other tracked folder:

- any path component starting with "." (the usual OS/tool convention for a
  folder that shouldn't be shown casually), or
- a folder containing a `.mediavault-hide` marker file -- for a folder you
  want hidden from browsing without renaming it.

Hiding is purely a browse-view concern. It never affects scanning, sync,
duplicate detection, or protection tracking -- a hidden folder is indexed
and backed up exactly like any other.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

HIDE_MARKER = ".mediavault-hide"


def _parts(rel_path: str) -> tuple[str, ...]:
    return PurePosixPath(rel_path).parts


def has_hidden_component(rel_path: str) -> bool:
    """True if any folder (or the file itself) in rel_path starts with '.'."""
    return any(part.startswith(".") for part in _parts(rel_path))


def marker_folders(rel_paths: list[str]) -> set[str]:
    """Root-relative folder paths containing a .mediavault-hide marker file.
    "" means the marker sits at the root itself, hiding everything."""
    hidden = set()
    for rel_path in rel_paths:
        p = PurePosixPath(rel_path)
        if p.name == HIDE_MARKER:
            hidden.add("" if str(p.parent) == "." else str(p.parent))
    return hidden


def is_under_marker(rel_path: str, hidden_marker_folders: set[str]) -> bool:
    if "" in hidden_marker_folders:
        return True
    cur = PurePosixPath("")
    for part in _parts(rel_path)[:-1]:
        cur = cur / part
        if str(cur) in hidden_marker_folders:
            return True
    return False


def is_hidden(rel_path: str, hidden_marker_folders: set[str]) -> bool:
    return has_hidden_component(rel_path) or is_under_marker(rel_path, hidden_marker_folders)


@dataclass
class FolderNode:
    name: str
    path: str
    folders: dict = field(default_factory=dict)  # str -> FolderNode
    files: list = field(default_factory=list)  # list[sqlite3.Row-like]

    def sorted_folders(self):
        return sorted(self.folders.values(), key=lambda f: f.name.lower())

    def sorted_files(self):
        return sorted(self.files, key=lambda f: f["rel_path"].lower())

    def total_videos(self) -> int:
        return len(self.files) + sum(child.total_videos() for child in self.folders.values())


def build_tree(files, video_extensions: list[str]) -> FolderNode:
    """Builds a folder tree of playable video files, skipping hidden ones.
    `files` are rows (or dicts) from a single root with rel_path/missing/
    is_placeholder columns -- callers should scope this to one root_label at
    a time, since rel_path is only unique within a root."""
    rel_paths = [f["rel_path"] for f in files]
    hidden_marker_folders = marker_folders(rel_paths)
    exts = {e.lower() for e in video_extensions}

    root = FolderNode(name="", path="")
    for f in files:
        if f["missing"] or f["is_placeholder"]:
            continue
        rel_path = f["rel_path"]
        if PurePosixPath(rel_path).suffix.lower() not in exts:
            continue
        if is_hidden(rel_path, hidden_marker_folders):
            continue

        parts = _parts(rel_path)
        node = root
        cur_path = ""
        for part in parts[:-1]:
            cur_path = f"{cur_path}/{part}" if cur_path else part
            node = node.folders.setdefault(part, FolderNode(name=part, path=cur_path))
        node.files.append(f)
    return root
