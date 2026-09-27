import json

import pytest

from mediavault import catalog, protection, scanner
from .test_scanner import make_root
from .conftest import write_file


def test_export_then_import_merges_cross_machine(conn, tmp_path):
    # Machine A: scans a primary with one file, exports its catalog.
    primary = make_root(tmp_path, label="primary", role="primary", mode="mirror")
    write_file(primary.path, "Movie.mkv", b"shared content")
    scanner.scan_root(conn, primary, hash_algo="sha256")

    out = tmp_path / "machine-a.json"
    n = catalog.export_catalog(conn, "machine-a", out)
    assert n == 1

    exported = json.loads(out.read_text())
    assert exported["machine"] == "machine-a"
    assert exported["files"][0]["rel_path"] == "Movie.mkv"

    # Before importing anything, this one copy is at risk.
    report = protection.compute_protection(conn, "machine-a")
    assert report["at_risk"], "expected the single copy to be at risk before any import"

    # Machine B independently has the exact same file (same hash) on its own
    # mirror root -- simulate its exported catalog without needing it mounted.
    remote_catalog = {
        "machine": "machine-b",
        "exported_at": "2026-01-01T00:00:00Z",
        "files": [
            {
                "root_label": "backup",
                "role": "backup",
                "mode": "mirror",
                "rel_path": "Movie.mkv",
                "size": len(b"shared content"),
                "hash": exported["files"][0]["hash"],
            }
        ],
    }
    imported_count = catalog.import_catalog(conn, remote_catalog, local_machine="machine-a")
    assert imported_count == 1

    report = protection.compute_protection(conn, "machine-a")
    assert report["covered"] == 1
    assert report["at_risk"] == []
    assert "machine-b" in report["known_machines"]


def test_import_rejects_catalog_from_same_machine(conn, tmp_path):
    primary = make_root(tmp_path, label="primary", role="primary", mode="mirror")
    write_file(primary.path, "Movie.mkv", b"content")
    scanner.scan_root(conn, primary, hash_algo="sha256")

    data = catalog.build_catalog(conn, "this-machine")
    with pytest.raises(catalog.CatalogError):
        catalog.import_catalog(conn, data, local_machine="this-machine")


def test_load_catalog_file_rejects_garbage(tmp_path):
    bad = tmp_path / "not-a-catalog.json"
    bad.write_text('{"hello": "world"}')
    with pytest.raises(catalog.CatalogError):
        catalog.load_catalog_file(bad)


def test_reimporting_same_machine_replaces_not_duplicates(conn, tmp_path):
    remote = {"machine": "machine-b", "exported_at": None, "files": [
        {"root_label": "backup", "role": "backup", "mode": "mirror", "rel_path": "A.mkv", "size": 10, "hash": "h1"},
    ]}
    catalog.import_catalog(conn, remote, local_machine="machine-a")
    remote2 = {"machine": "machine-b", "exported_at": None, "files": [
        {"root_label": "backup", "role": "backup", "mode": "mirror", "rel_path": "B.mkv", "size": 20, "hash": "h2"},
    ]}
    catalog.import_catalog(conn, remote2, local_machine="machine-a")

    from mediavault import db
    remote_files = conn.execute("SELECT rel_path FROM remote_files WHERE machine='machine-b'").fetchall()
    assert [r["rel_path"] for r in remote_files] == ["B.mkv"]
