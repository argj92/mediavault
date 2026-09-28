"""Exercises peers.py's initiate/accept/sync flow the same way manual
testing did (two machines, a real pairing handshake, a real catalog fetch)
but without real sockets/threads: requests.post/get are monkeypatched to
route straight into peer_api.app via TestClient, swapping which machine's
config is "currently serving" immediately before each call -- safe because
these are synchronous, sequential calls, never concurrent.
"""
from fastapi.testclient import TestClient

from mediavault import db, peer_api, peers, scanner
from mediavault.config import AppConfig
from mediavault.discovery import PeerInfo
from .test_scanner import make_root
from .conftest import write_file

TEST_CLIENT = TestClient(peer_api.app)


def _route(monkeypatch, address_to_config: dict):
    """Redirects peers.py's outbound HTTP calls to the right simulated
    machine's peer_api, based on the address in the URL."""

    def fake_post(url, json=None, timeout=None):
        for address, config in address_to_config.items():
            if f"://{address}:" in url:
                peer_api.app.state.config = config
                path = url.split(f":{config.peer_port}", 1)[1]
                return TEST_CLIENT.post(path, json=json)
        raise AssertionError(f"no simulated machine for {url}")

    def fake_get(url, headers=None, timeout=None):
        for address, config in address_to_config.items():
            if f"://{address}:" in url:
                peer_api.app.state.config = config
                path = url.split(f":{config.peer_port}", 1)[1]
                return TEST_CLIENT.get(path, headers=headers)
        raise AssertionError(f"no simulated machine for {url}")

    monkeypatch.setattr(peers.requests, "post", fake_post)
    monkeypatch.setattr(peers.requests, "get", fake_get)
    monkeypatch.setattr(peers, "_own_address", lambda: "machine-a-address")


def test_full_pairing_and_sync_round_trip(tmp_path, monkeypatch):
    config_a = AppConfig(
        machine="machine-a", db_path=tmp_path / "a.db", hash_algo="sha256",
        tmdb_api_key_env="X", video_extensions=[".mkv"], scan_interval_minutes=15, peer_port=9001,
    )
    config_b = AppConfig(
        machine="machine-b", db_path=tmp_path / "b.db", hash_algo="sha256",
        tmdb_api_key_env="X", video_extensions=[".mkv"], scan_interval_minutes=15, peer_port=9002,
    )
    with db.connect(config_a.db_path) as conn:
        db.init_db(conn)
        peer_id_a = db.get_or_create_peer_id(conn)
    with db.connect(config_b.db_path) as conn:
        db.init_db(conn)
        peer_id_b = db.get_or_create_peer_id(conn)

    _route(monkeypatch, {"machine-b-address": config_b, "machine-a-address": config_a})

    # A initiates pairing with B.
    peer_b = PeerInfo(peer_id=peer_id_b, machine_name="machine-b", address="machine-b-address", port=9002)
    with db.connect(config_a.db_path) as conn_a:
        code = peers.initiate_pairing(conn_a, config_a, peer_b)

    # B sees it pending, with the same code, and accepts.
    with db.connect(config_b.db_path) as conn_b:
        pending = db.list_pending_pairings(conn_b)
        assert len(pending) == 1
        assert pending[0]["code"] == code
        peers.accept_pending_pairing(conn_b, config_b, peer_id_a)

    # Both sides are now paired, with cross-matching tokens.
    with db.connect(config_a.db_path) as conn_a:
        a_view = dict(db.get_known_peer(conn_a, peer_id_b))
    with db.connect(config_b.db_path) as conn_b:
        b_view = dict(db.get_known_peer(conn_b, peer_id_a))
    assert a_view["paired"] and b_view["paired"]
    assert a_view["outgoing_token"] == b_view["incoming_token"]
    assert b_view["outgoing_token"] == a_view["incoming_token"]

    # A has a real tracked file; B syncs and imports it over the (simulated) API.
    root = make_root(tmp_path, label="a-primary", role="primary", mode="mirror")
    write_file(root.path, "Movie.mkv", b"peer sync content")
    with db.connect(config_a.db_path) as conn_a:
        db.upsert_root(conn_a, root.label, str(root.path), root.role, root.mode)
        scanner.scan_root(conn_a, root, hash_algo="sha256")

    with db.connect(config_b.db_path) as conn_b:
        peer_row = db.get_known_peer(conn_b, peer_id_a)
        n = peers.sync_with_peer(conn_b, config_b, peer_row)
        assert n == 1
        remote_files = conn_b.execute("SELECT rel_path FROM remote_files WHERE machine='machine-a'").fetchall()
        assert [r["rel_path"] for r in remote_files] == ["Movie.mkv"]
        assert db.get_known_peer(conn_b, peer_id_a)["last_synced_at"] is not None
