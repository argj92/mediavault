"""Visibility into the OS-level Trash/Recycle Bin, so big files sitting there
(often forgotten about) show up alongside mediavault's own duplicate
quarantine when you're trying to reclaim disk space.

Emptying anything here is real, permanent deletion — this module only ever
acts when explicitly told to (confirm=True), and mediavault's own background
scanner never touches it automatically.
"""
from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TrashItem:
    path: Path
    size: int
    source: str  # which trash location it came from


@dataclass
class TrashAccessError:
    location: Path
    error: str


def trash_locations() -> list[Path]:
    system = platform.system()
    home = Path.home()
    locations: list[Path] = []

    if system == "Darwin":
        locations.append(home / ".Trash")
        # Other mounted volumes each have their own per-user trash.
        volumes = Path("/Volumes")
        if volumes.exists():
            for vol in volumes.iterdir():
                trashes = vol / ".Trashes" / str(os.getuid())
                if trashes.exists():
                    locations.append(trashes)
    elif system == "Linux":
        locations.append(home / ".local" / "share" / "Trash" / "files")
    elif system == "Windows":
        for drive in "CDEFGH":
            candidate = Path(f"{drive}:/$Recycle.Bin")
            if candidate.exists():
                locations.append(candidate)

    return [p for p in locations if p.exists()]


def _dir_size(path: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            fp = Path(dirpath) / name
            try:
                total += fp.stat().st_size
            except OSError:
                pass
    return total


def scan_trash(top_n: int = 10_000) -> dict:
    """The real scan: returns items found *and* any location mediavault
    couldn't read, so a permission problem is never silently reported as
    "trash is empty" — those look identical unless you check for both."""
    items: list[TrashItem] = []
    access_errors: list[TrashAccessError] = []

    for loc in trash_locations():
        try:
            entries = list(loc.iterdir())
        except OSError as exc:
            access_errors.append(TrashAccessError(location=loc, error=str(exc)))
            continue
        for entry in entries:
            try:
                size = entry.stat().st_size if entry.is_file() else _dir_size(entry)
            except OSError as exc:
                access_errors.append(TrashAccessError(location=entry, error=str(exc)))
                continue
            items.append(TrashItem(path=entry, size=size, source=str(loc)))

    items.sort(key=lambda i: i.size, reverse=True)
    return {
        "items": items[:top_n],
        "total_bytes": sum(i.size for i in items),
        "access_errors": access_errors,
        # False whenever any location/item couldn't be read — the totals
        # above are then a floor, not the real total.
        "accurate": not access_errors,
    }


def list_trash_items(top_n: int = 50) -> list[TrashItem]:
    return scan_trash(top_n=top_n)["items"]


def total_trash_bytes() -> int:
    return scan_trash()["total_bytes"]


def empty_items(paths: list[str], confirm: bool = False) -> dict:
    """Permanently deletes the given trash items. Requires confirm=True —
    callers (CLI/web) must have already gotten explicit user confirmation for
    this specific batch; this is irreversible."""
    if not confirm:
        raise ValueError("empty_items requires confirm=True — this permanently deletes files")

    deleted, errors = [], []
    for p in paths:
        path = Path(p)
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            deleted.append(p)
        except OSError as exc:
            errors.append(f"{p}: {exc}")
    return {"deleted": deleted, "errors": errors}
