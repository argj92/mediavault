"""Content hashing with a fast-path that avoids re-reading unchanged files."""
from __future__ import annotations

import hashlib
from pathlib import Path

try:
    import blake3  # type: ignore

    _HAVE_BLAKE3 = True
except ImportError:
    _HAVE_BLAKE3 = False

CHUNK_SIZE = 4 * 1024 * 1024


def resolve_algo(requested: str) -> str:
    """blake3 is much faster on large video files; fall back silently if unavailable."""
    if requested == "blake3" and not _HAVE_BLAKE3:
        return "sha256"
    return requested


def hash_file(path: Path, algo: str = "blake3") -> str:
    algo = resolve_algo(algo)
    hasher = blake3.blake3() if algo == "blake3" else hashlib.new(algo)
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            hasher.update(chunk)
    return f"{algo}:{hasher.hexdigest()}"


def quick_signature(path: Path) -> tuple[int, float]:
    """(size, mtime) — cheap to obtain, used to decide whether a cached hash
    is still valid without re-reading file contents."""
    st = path.stat()
    return st.st_size, st.st_mtime
