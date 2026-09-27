"""Cross-machine "protection" view: for each unique file (by content hash),
how many independent mirror-mode copies exist — locally, and on other
machines too if their catalogs have been imported (see catalog.py) — without
needing every machine mounted at once.

Only `mirror`-mode roots count toward protection, matching the same
distinction already made for sync (sync.py's is_deletion_source): a
storage-optimized iCloud copy (`subset`), or a Downloads-style inbox
(`watch`), is real content worth knowing about but isn't a backup. A file
backed by exactly one mirror copy is "at risk" — the whole point of a
second machine/drive is that losing one doesn't lose the file.
"""
from __future__ import annotations

import sqlite3


def _local_mirror_files(conn: sqlite3.Connection, machine: str) -> list[dict]:
    rows = conn.execute(
        """SELECT f.hash, f.size, f.root_label, f.rel_path
           FROM files f JOIN roots r ON r.label = f.root_label
           WHERE f.missing=0 AND f.is_placeholder=0 AND f.hash IS NOT NULL
             AND r.mode='mirror' AND r.enabled=1"""
    ).fetchall()
    return [
        {
            "machine": machine,
            "root_label": r["root_label"],
            "rel_path": r["rel_path"],
            "hash": r["hash"],
            "size": r["size"],
        }
        for r in rows
    ]


def _remote_mirror_files(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT machine, root_label, rel_path, size, hash FROM remote_files WHERE mode='mirror' AND hash IS NOT NULL"
    ).fetchall()
    return [dict(r) for r in rows]


def compute_protection(conn: sqlite3.Connection, local_machine: str) -> dict:
    """Returns {total_groups, covered, at_risk (list, largest first),
    known_machines}. `at_risk` entries: {hash, size, protective_count,
    locations: [{machine, root_label, rel_path}]}."""
    all_files = _local_mirror_files(conn, local_machine) + _remote_mirror_files(conn)

    by_hash: dict[str, dict] = {}
    for f in all_files:
        g = by_hash.setdefault(f["hash"], {"hash": f["hash"], "size": 0, "locations": []})
        g["locations"].append({"machine": f["machine"], "root_label": f["root_label"], "rel_path": f["rel_path"]})
        g["size"] = max(g["size"], f["size"] or 0)

    groups = []
    for g in by_hash.values():
        distinct_locations = {(loc["machine"], loc["root_label"]) for loc in g["locations"]}
        groups.append({**g, "protective_count": len(distinct_locations)})

    at_risk = sorted((g for g in groups if g["protective_count"] < 2), key=lambda g: g["size"], reverse=True)

    return {
        "total_groups": len(groups),
        "covered": len(groups) - len(at_risk),
        "at_risk": at_risk,
        "known_machines": sorted({loc["machine"] for g in groups for loc in g["locations"]}),
    }
