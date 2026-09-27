from mediavault import scheduler
from mediavault.config import AppConfig

from .conftest import write_file
from .test_scanner import make_root


def make_config(tmp_path) -> AppConfig:
    return AppConfig(
        machine="test-machine",
        db_path=tmp_path / "mediavault.db",
        hash_algo="sha256",
        tmdb_api_key_env="TMDB_API_KEY",
        video_extensions=[".mkv"],
        scan_interval_minutes=15,
    )


def test_reconcile_skips_root_missing_entirely(conn, tmp_path):
    # A removable drive that isn't plugged in at all right now.
    ghost = make_root(tmp_path, label="ghost", role="backup", mode="mirror")
    good = make_root(tmp_path, label="good", role="primary", mode="mirror")
    write_file(good.path, "Movie.mkv", b"content")

    from mediavault import db

    db.upsert_root(conn, ghost.label, str(ghost.path), ghost.role, ghost.mode)
    db.upsert_root(conn, good.label, str(good.path), good.role, good.mode)
    ghost.path.rmdir()  # simulate "not mounted" -- path no longer exists

    summary = scheduler.reconcile_once(conn, make_config(tmp_path))

    assert len(summary["scan_errors"]) == 1
    assert "ghost" in summary["scan_errors"][0]
    # The good root should still have been scanned despite ghost failing.
    row = db.get_file(conn, good.label, "Movie.mkv")
    assert row is not None


def test_reconcile_survives_mid_scan_disconnect(conn, tmp_path, monkeypatch):
    """A network mount that drops partway through a walk (stale SMB/NFS
    handle, sleep/wake, Wi-Fi hiccup) raises some OSError other than
    FileNotFoundError. That must not abort scanning of the remaining roots."""
    flaky = make_root(tmp_path, label="flaky", role="backup", mode="mirror")
    good = make_root(tmp_path, label="good", role="primary", mode="mirror")
    write_file(flaky.path, "Movie.mkv", b"content")
    write_file(good.path, "Movie.mkv", b"content")

    from mediavault import db

    db.upsert_root(conn, flaky.label, str(flaky.path), flaky.role, flaky.mode)
    db.upsert_root(conn, good.label, str(good.path), good.role, good.mode)

    from mediavault import cloud as cloud_mod

    original_walk = cloud_mod.walk_cloud_aware

    def flaky_walk(root):
        if root == flaky.path:
            raise ConnectionError("stale NFS file handle")
        return original_walk(root)

    monkeypatch.setattr(cloud_mod, "walk_cloud_aware", flaky_walk)

    summary = scheduler.reconcile_once(conn, make_config(tmp_path))

    assert len(summary["scan_errors"]) == 1
    assert "stale NFS file handle" in summary["scan_errors"][0]
    # The good root should still have been scanned despite flaky's disconnect.
    row = db.get_file(conn, good.label, "Movie.mkv")
    assert row is not None
