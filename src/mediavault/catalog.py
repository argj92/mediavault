"""Portable per-machine catalog export/import — the cross-machine trick
borrowed from a "Media Map" tool a friend built: each machine dumps a small
JSON manifest (hash, size, path, role/mode — never the media itself) that
another machine can import, so protection.py can compute cross-machine
coverage without both machines being mounted at the same time. No network
access, no automated multi-machine collection — you move the file yourself
(AirDrop, a synced folder, a USB drive), same as that tool's own model.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import db


class CatalogError(ValueError):
    pass


def build_catalog(conn: sqlite3.Connection, machine: str) -> dict:
    rows = conn.execute(
        """SELECT f.root_label, r.role, r.mode, f.rel_path, f.size, f.hash
           FROM files f JOIN roots r ON r.label = f.root_label
           WHERE f.missing=0 AND f.is_placeholder=0 AND f.hash IS NOT NULL"""
    ).fetchall()
    return {
        "machine": machine,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": [
            {
                "root_label": r["root_label"],
                "role": r["role"],
                "mode": r["mode"],
                "rel_path": r["rel_path"],
                "size": r["size"],
                "hash": r["hash"],
            }
            for r in rows
        ],
    }


def export_catalog(conn: sqlite3.Connection, machine: str, out_path: Path) -> int:
    catalog = build_catalog(conn, machine)
    out_path.write_text(json.dumps(catalog, indent=2))
    return len(catalog["files"])


def load_catalog_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"Could not read {path} as a mediavault catalog: {exc}") from exc
    if not isinstance(data, dict) or "machine" not in data or "files" not in data:
        raise CatalogError(f"{path} doesn't look like a mediavault catalog (missing 'machine'/'files').")
    return data


def import_catalog(conn: sqlite3.Connection, data: dict, local_machine: str) -> int:
    if data.get("machine") == local_machine:
        raise CatalogError(
            f"That catalog is from this same machine ({local_machine}) — import one from another machine."
        )
    db.save_remote_catalog(conn, data["machine"], data.get("exported_at"), data["files"])
    return len(data["files"])
