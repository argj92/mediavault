"""Exercises peers.py's initiate/accept/sync flow the same way manual
testing did (two machines, a real pairing handshake, a real catalog fetch)
but without real sockets/threads: requests.post/get are monkeypatched to
route straight into peer_api.app via TestClient, swapping which machine's
config is "currently serving" immediately before each call -- safe because
these are synchronous, sequential calls, never concurrent.
"""
import pytest
from fastapi.testclient import TestClient

from mediavault import db, peer_api, peers, scanner
from mediavault.config import AppConfig
from mediavault.discovery import PeerInfo
from .test_scanner import make_root
from .conftest import write_file

def _route(monkeypatch, address_to_config: dict):
    """Redirects peers.py's outbound HTTP calls to the right simulated
    machine's peer_api, based on the address in the URL. Each call arrives
    with the *other* machine's address as its source IP, like on a real LAN."""
    addresses = list(address_to_config)
    clients = {
        target: TestClient(peer_api.app, client=([a for a in addresses if a != target][0], 40000))
        for target in addresses
    }

    def _dispatch(method, url, **kw):
        for address, config in address_to_config.items():
            if f"://{address}:" in url:
                peer_api.app.state.config = config
                path = url.split(f":{config.peer_port}", 1)[1]
                return getattr(clients[address], method)(path, **kw)
        raise peers.requests.ConnectionError(f"no simulated machine for {url}")

    monkeypatch.setattr(peers.requests, "post", lambda url, json=None, timeout=None: _dispatch("post", url, json=json))
    monkeypatch.setattr(peers.requests, "get", lambda url, headers=None, timeout=None: _dispatch("get", url, headers=headers))
    monkeypatch.setattr(peers, "_own_address", lambda: "machine-a-address")


def _two_machines(tmp_path, monkeypatch, lan=True):
    configs = {}
    ids = {}
    for name, port in (("machine-a", 9001), ("machine-b", 9002)):
        configs[name] = AppConfig(
            machine=name, db_path=tmp_path / f"{name}.db", hash_algo="sha256", tmdb_api_key_env="X",
            video_extensions=[".mkv"], scan_interval_minutes=15, peer_port=port, enable_lan_discovery=lan,
        )
        with db.connect(configs[name].db_path) as conn:
            db.init_db(conn)
            ids[name] = db.get_or_create_peer_id(conn)
    _route(monkeypatch, {"machine-b-address": configs["machine-b"], "machine-a-address": configs["machine-a"]})
    peer_b = PeerInfo(peer_id=ids["machine-b"], machine_name="machine-b", address="machine-b-address", port=9002)
    return configs["machine-a"], configs["machine-b"], ids["machine-a"], ids["machine-b"], peer_b


def test_full_pairing_and_sync_round_trip(tmp_path, monkeypatch):
    config_a, config_b, peer_id_a, peer_id_b, peer_b = _two_machines(tmp_path, monkeypatch)

    # A initiates pairing with B.
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


def test_acceptor_records_the_initiators_real_source_address(tmp_path, monkeypatch):
    config_a, config_b, peer_id_a, peer_id_b, peer_b = _two_machines(tmp_path, monkeypatch)
    with db.connect(config_a.db_path) as conn:
        peers.initiate_pairing(conn, config_a, peer_b)
    with db.connect(config_b.db_path) as conn:
        assert db.get_pending_pairing(conn, peer_id_a)["address"] == "machine-a-address"


def test_initiating_to_an_unreachable_peer_leaves_no_half_made_pairing(tmp_path, monkeypatch):
    config_a, _, _, _, peer_b = _two_machines(tmp_path, monkeypatch)
    monkeypatch.setattr(peers.requests, "post", lambda *a, **k: (_ for _ in ()).throw(peers.requests.ConnectionError("down")))

    with db.connect(config_a.db_path) as conn:
        with pytest.raises(peers.requests.ConnectionError):
            peers.initiate_pairing(conn, config_a, peer_b)
        assert db.list_known_peers(conn) == []


def test_failed_accept_callback_does_not_leave_us_paired(tmp_path, monkeypatch):
    """The acceptor used to record 'paired' before telling the initiator its
    token; a failed callback then left it paired with a peer that couldn't
    authenticate -- and the pending request gone, so no retry."""
    config_a, config_b, peer_id_a, _, peer_b = _two_machines(tmp_path, monkeypatch)
    with db.connect(config_a.db_path) as conn:
        peers.initiate_pairing(conn, config_a, peer_b)
    real_post = peers.requests.post

    def a_is_down(url, **kw):
        raise peers.requests.ConnectionError("a is unreachable")

    monkeypatch.setattr(peers.requests, "post", a_is_down)
    with db.connect(config_b.db_path) as conn:
        with pytest.raises(peers.requests.ConnectionError):
            peers.accept_pending_pairing(conn, config_b, peer_id_a)
        assert db.get_known_peer(conn, peer_id_a) is None
        assert db.get_pending_pairing(conn, peer_id_a) is not None   # can retry

    monkeypatch.setattr(peers.requests, "post", real_post)
    with db.connect(config_b.db_path) as conn:
        peers.accept_pending_pairing(conn, config_b, peer_id_a)
        assert db.get_known_peer(conn, peer_id_a)["paired"] == 1


def test_repairing_an_already_paired_peer_is_refused_and_changes_nothing(tmp_path, monkeypatch):
    config_a, config_b, peer_id_a, peer_id_b, peer_b = _two_machines(tmp_path, monkeypatch)
    with db.connect(config_a.db_path) as conn:
        peers.initiate_pairing(conn, config_a, peer_b)
    with db.connect(config_b.db_path) as conn:
        peers.accept_pending_pairing(conn, config_b, peer_id_a)
    with db.connect(config_a.db_path) as conn:
        before = dict(db.get_known_peer(conn, peer_id_b))
        with pytest.raises(peers.PairingError, match="Already paired"):
            peers.initiate_pairing(conn, config_a, peer_b)
        assert dict(db.get_known_peer(conn, peer_id_b)) == before   # tokens intact, still paired


def test_pairing_is_refused_when_lan_mode_is_off(tmp_path, monkeypatch):
    config_a, config_b, peer_id_a, _, peer_b = _two_machines(tmp_path, monkeypatch, lan=False)
    with db.connect(config_a.db_path) as conn:
        with pytest.raises(peers.PairingError, match="enable_lan_discovery"):
            peers.initiate_pairing(conn, config_a, peer_b)
    with db.connect(config_b.db_path) as conn:
        db.add_pending_pairing(conn, peer_id_a, "machine-a", "machine-a-address", 9001, "123456", "a" * 32)
        with pytest.raises(peers.PairingError, match="enable_lan_discovery"):
            peers.accept_pending_pairing(conn, config_b, peer_id_a)
