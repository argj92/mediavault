"""Finds media sitting in "inbox"/"watch" folders (e.g. Downloads) that isn't
tracked in any library root yet, so it can be suggested for filing away."""
from __future__ import annotations

import sqlite3


def untracked_media(conn: sqlite3.Connection, video_extensions: list[str]) -> list[dict]:
    inbox_roots = db_list_inbox_roots(conn)
    library_hashes = _library_hashes(conn)

    suggestions = []
    for root in inbox_roots:
        files = conn.execute(
            "SELECT * FROM files WHERE root_label=? AND missing=0 AND is_placeholder=0", (root["label"],)
        ).fetchall()
        for f in files:
            ext = "." + f["rel_path"].rsplit(".", 1)[-1].lower() if "." in f["rel_path"] else ""
            if ext not in video_extensions:
                continue
            already_in_library = f["hash"] in library_hashes
            suggestions.append(
                {
                    "root_label": root["label"],
                    "rel_path": f["rel_path"],
                    "size": f["size"],
                    "hash": f["hash"],
                    "already_in_library": already_in_library,
                }
            )
    return suggestions


def db_list_inbox_roots(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM roots WHERE role='inbox' AND enabled=1").fetchall()


def _library_hashes(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT hash FROM files WHERE missing=0 AND is_placeholder=0 "
        "AND root_label IN (SELECT label FROM roots WHERE role != 'inbox') AND hash IS NOT NULL"
    ).fetchall()
    return {r["hash"] for r in rows}
