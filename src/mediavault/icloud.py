"""What is using the space in an iCloud root, so something can be removed when
iCloud (or the Mac's disk) runs short -- by folder and by file, from the
index, without touching the files.

Sizes come from what the scanner recorded: a file that isn't downloaded still
has its real size (macOS reports it), so this counts what iCloud holds, not
just what is on this Mac's disk. For each big file it also says whether the
same file lives on another drive, which is the question that decides whether
deleting the iCloud copy loses anything.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import browse, db


@dataclass
class Folder:
    name: str
    path: str                      # root-relative, "" for the root
    bytes: int = 0                 # everything under it, in iCloud
    local_bytes: int = 0           # of which downloaded on this Mac
    files: int = 0
    children: dict[str, "Folder"] = field(default_factory=dict)

    def sorted_children(self) -> list["Folder"]:
        return sorted(self.children.values(), key=lambda f: (-f.bytes, f.name.lower()))


def build_folder_tree(rows) -> Folder:
    """Rows are (rel_path, size, is_placeholder). Every folder on the way to a
    file is credited with the file's size, so a folder's number is its whole
    subtree."""
    root = Folder(name="", path="")
    for rel_path, size, is_placeholder in rows:
        size = size or 0
        parts = PurePosixPath(rel_path).parts
        node = root
        node.bytes += size
        node.files += 1
        if not is_placeholder:
            node.local_bytes += size
        for i, part in enumerate(parts[:-1]):
            child = node.children.get(part)
            if child is None:
                child = node.children[part] = Folder(name=part, path="/".join(parts[: i + 1]))
            child.bytes += size
            child.files += 1
            if not is_placeholder:
                child.local_bytes += size
            node = child
    return root


def _copies_elsewhere(conn: sqlite3.Connection, root_label: str, row) -> list[dict]:
    """Where else this file is. Same content (hash) when the iCloud copy was
    ever hashed; otherwise same name and size, which is weaker -- it says so --
    because a never-downloaded file can't be hashed without downloading it."""
    found: dict[str, dict] = {}
    if row["hash"]:
        for r in conn.execute(
            "SELECT root_label, rel_path FROM files WHERE hash=? AND root_label != ? AND missing=0",
            (row["hash"], root_label),
        ):
            found[r["root_label"]] = {"root": r["root_label"], "path": r["rel_path"], "match": "content"}
    name = PurePosixPath(row["rel_path"]).name
    if row["size"]:
        for r in conn.execute(
            "SELECT root_label, rel_path FROM files WHERE size=? AND root_label != ? AND missing=0 AND rel_path LIKE ?",
            (row["size"], root_label, f"%{name}"),
        ):
            if PurePosixPath(r["rel_path"]).name == name and r["root_label"] not in found:
                found[r["root_label"]] = {"root": r["root_label"], "path": r["rel_path"], "match": "name and size"}
    return sorted(found.values(), key=lambda c: (c["match"] != "content", c["root"]))


def _visible_rows(conn, root_label: str, hidden_marker_dirs: set[str]):
    shown, hidden_bytes = [], 0
    for r in db.all_files(conn, root_label=root_label):
        rel = r["rel_path"]
        is_hidden = browse.has_hidden_component(rel) or any(
            d == "" or rel == d or rel.startswith(d + "/") for d in hidden_marker_dirs
        )
        if is_hidden:
            hidden_bytes += r["size"] or 0
        else:
            shown.append(r)
    return shown, hidden_bytes


def analyze(conn: sqlite3.Connection, root_label: str, top_files: int = 25) -> dict:
    all_rows = db.all_files(conn, root_label=root_label)
    marker_dirs = browse.marker_folders([r["rel_path"] for r in all_rows])
    rows, hidden_bytes = _visible_rows(conn, root_label, marker_dirs)
    tree = build_folder_tree((r["rel_path"], r["size"], r["is_placeholder"]) for r in rows)
    biggest = sorted((r for r in rows if r["size"]), key=lambda r: -r["size"])[:top_files]
    files = []
    for r in biggest:
        elsewhere = _copies_elsewhere(conn, root_label, r)
        files.append({
            "rel_path": r["rel_path"],
            "folder": str(PurePosixPath(r["rel_path"]).parent) if "/" in r["rel_path"] else "",
            "name": PurePosixPath(r["rel_path"]).name,
            "size": r["size"],
            "local": not r["is_placeholder"],
            "elsewhere": elsewhere,
            "only_copy": not elsewhere,
        })
    return {
        "tree": tree,
        "files": files,
        "total_bytes": tree.bytes,
        "local_bytes": tree.local_bytes,
        "cloud_only_bytes": tree.bytes - tree.local_bytes,
        "file_count": tree.files,
        "unknown_size": sum(1 for r in rows if not r["size"]),
        "hidden_bytes": hidden_bytes,
        "only_copy_bytes": sum(f["size"] for f in files if f["only_copy"]),
    }


def resolve_inside(root_path: str, rel_path: str) -> Path | None:
    """root/rel_path if it stays inside the root (no '..' tricks), else None."""
    root = Path(root_path).resolve()
    target = (root / rel_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target


def is_known_path(conn: sqlite3.Connection, root_label: str, rel_path: str) -> bool:
    """A file or folder the index knows under this root."""
    rel_path = rel_path.strip("/")
    if not rel_path:
        return True
    row = conn.execute(
        "SELECT 1 FROM files WHERE root_label=? AND missing=0 AND (rel_path=? OR rel_path LIKE ? ESCAPE '\\') LIMIT 1",
        (root_label, rel_path, rel_path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"),
    ).fetchone()
    return row is not None
