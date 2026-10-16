import pytest
from fastapi.testclient import TestClient

from mediavault import db, pairing, peer_api, scanner
from mediavault.config import AppConfig
from .test_scanner import make_root
from .conftest import write_file


@pytest.fixture()
def client(tmp_path):
    config = AppConfig(
        machine="test-machine", db_path=tmp_path / "mediavault.db", hash_algo="sha256",
        tmdb_api_key_env="X", video_extensions=[".mkv"], scan_interval_minutes=15,
    )
    with db.connect(config.db_path) as conn:
        db.init_db(conn)
    peer_api.app.state.config = config
    return TestClient(peer_api.app), config


def test_identity_is_public_and_stable(client):
    c, config = client
    r1 = c.get("/peer/identity")
    r2 = c.get("/peer/identity")
    assert r1.status_code == 200
    assert r1.json()["machine_name"] == "test-machine"
    assert r1.json()["peer_id"] == r2.json()["peer_id"]


TOK_A = "a" * 32
TOK_B = "b" * 32


def _req(**over):
    body = {"peer_id": "peer-a", "machine_name": "machine-a", "port": 8421, "code": "123456", "token": TOK_A}
    body.update(over)
    return body


def _confirm(initiator_token, peer_id="peer-b", token=TOK_B, proof=None):
    return {"peer_id": peer_id, "token": token, "proof": proof or pairing.pairing_proof(initiator_token, peer_id, token)}


def _initiated(config, peer_id="peer-b", token=TOK_A):
    with db.connect(config.db_path) as conn:
        db.upsert_pairing_peer(conn, peer_id, "machine-b", "10.0.0.2", 8421, incoming_token=token, paired=False)


def test_pair_request_rejects_missing_or_malformed_fields(client):
    c, _ = client
    assert c.post("/peer/pair-request", json={"peer_id": "abc"}).status_code == 422
    for bad in (
        {"code": "12"}, {"code": "abcdef"}, {"token": "short"}, {"port": 0}, {"port": 70000},
        {"machine_name": ""}, {"machine_name": "x" * 65}, {"machine_name": "evil\nname"},
        {"peer_id": "has space"}, {"peer_id": "x" * 65},
    ):
        assert c.post("/peer/pair-request", json=_req(**bad)).status_code == 422, bad


def test_pair_request_stores_pending_with_the_real_source_address(tmp_path):
    config = AppConfig(
        machine="test-machine", db_path=tmp_path / "mediavault.db", hash_algo="sha256",
        tmdb_api_key_env="X", video_extensions=[".mkv"], scan_interval_minutes=15,
    )
    with db.connect(config.db_path) as conn:
        db.init_db(conn)
    peer_api.app.state.config = config
    c = TestClient(peer_api.app, client=("10.0.0.9", 5555))

    # a claimed `address` is ignored: the callback target is where the request came from
    r = c.post("/peer/pair-request", json=_req(address="6.6.6.6"))

    assert r.status_code == 200 and r.json() == {"status": "pending"}
    with db.connect(config.db_path) as conn:
        (p,) = db.list_pending_pairings(conn)
    assert p["address"] == "10.0.0.9" and p["code"] == "123456" and p["port"] == 8421


def test_a_second_host_cannot_overwrite_a_waiting_request(tmp_path):
    config = AppConfig(
        machine="test-machine", db_path=tmp_path / "mediavault.db", hash_algo="sha256",
        tmdb_api_key_env="X", video_extensions=[".mkv"], scan_interval_minutes=15,
    )
    with db.connect(config.db_path) as conn:
        db.init_db(conn)
    peer_api.app.state.config = config
    real = TestClient(peer_api.app, client=("10.0.0.1", 1))
    attacker = TestClient(peer_api.app, client=("10.0.0.66", 1))

    assert real.post("/peer/pair-request", json=_req()).status_code == 200
    squat = attacker.post("/peer/pair-request", json=_req(code="999999", token="c" * 32))

    assert squat.status_code == 409
    with db.connect(config.db_path) as conn:
        (p,) = db.list_pending_pairings(conn)
    assert p["code"] == "123456" and p["outgoing_token"] == TOK_A and p["address"] == "10.0.0.1"
    # the legitimate sender can still refresh its own request
    assert real.post("/peer/pair-request", json=_req(code="654321")).status_code == 200


def test_pair_request_cannot_rekey_an_already_paired_peer(client):
    c, config = client
    with db.connect(config.db_path) as conn:
        db.complete_pairing_as_acceptor(conn, "peer-a", "machine-a", "10.0.0.1", 8421, outgoing_token="o", incoming_token="i")

    assert c.post("/peer/pair-request", json=_req()).status_code == 409
    with db.connect(config.db_path) as conn:
        assert db.list_pending_pairings(conn) == []
        assert db.get_known_peer(conn, "peer-a")["incoming_token"] == "i"


def test_pending_requests_are_capped_and_expire(client):
    c, config = client
    for i in range(pairing.MAX_PENDING_PER_SOURCE):
        assert c.post("/peer/pair-request", json=_req(peer_id=f"p{i}")).status_code == 200
    assert c.post("/peer/pair-request", json=_req(peer_id="one-too-many")).status_code == 429

    with db.connect(config.db_path) as conn:
        conn.execute("UPDATE pending_pairings SET requested_at = requested_at - ?", (pairing.PENDING_TTL_SECONDS + 5,))
    assert c.post("/peer/pair-request", json=_req(peer_id="fresh")).status_code == 200
    with db.connect(config.db_path) as conn:
        assert [p["peer_id"] for p in db.list_pending_pairings(conn)] == ["fresh"]


def test_cannot_pair_with_yourself(client):
    c, config = client
    own = c.get("/peer/identity").json()["peer_id"]
    assert c.post("/peer/pair-request", json=_req(peer_id=own)).status_code == 400


def test_pair_confirm_requires_pairing_in_progress(client):
    c, _ = client
    r = c.post("/peer/pair-confirm", json=_confirm(TOK_A, peer_id="never-initiated"))
    assert r.status_code == 404


def test_pair_confirm_finalizes_initiated_pairing(client):
    c, config = client
    _initiated(config)

    r = c.post("/peer/pair-confirm", json=_confirm(TOK_A))

    assert r.status_code == 200 and r.json() == {"status": "paired"}
    with db.connect(config.db_path) as conn:
        row = db.get_known_peer(conn, "peer-b")
    assert row["paired"] == 1 and row["outgoing_token"] == TOK_B and row["incoming_token"] == TOK_A


def test_pair_confirm_without_proof_of_the_request_is_refused(client):
    """Anyone on the LAN can guess/learn a peer_id; only the machine that
    received our pair-request knows the token we sent it."""
    c, config = client
    _initiated(config)

    forged = c.post("/peer/pair-confirm", json=_confirm("f" * 32))   # proof keyed by the wrong secret
    assert forged.status_code == 403
    tampered = c.post("/peer/pair-confirm", json={**_confirm(TOK_A), "token": "d" * 32})  # token swapped after proving
    assert tampered.status_code == 403

    with db.connect(config.db_path) as conn:
        row = db.get_known_peer(conn, "peer-b")
    assert row["paired"] == 0 and row["outgoing_token"] is None


def test_pair_confirm_is_single_use(client):
    c, config = client
    _initiated(config)
    assert c.post("/peer/pair-confirm", json=_confirm(TOK_A)).status_code == 200
    # replaying (or racing) a second confirm must not swap the token we now present
    replay = c.post("/peer/pair-confirm", json=_confirm(TOK_A, token="e" * 32))
    assert replay.status_code == 404
    with db.connect(config.db_path) as conn:
        assert db.get_known_peer(conn, "peer-b")["outgoing_token"] == TOK_B


def test_stale_initiated_pairing_expires(client):
    c, config = client
    _initiated(config)
    with db.connect(config.db_path) as conn:
        conn.execute("UPDATE known_peers SET last_seen = last_seen - ?", (pairing.PENDING_TTL_SECONDS + 5,))
    assert c.post("/peer/pair-confirm", json=_confirm(TOK_A)).status_code == 404


def test_catalog_requires_bearer_token(client):
    c, _ = client
    assert c.get("/peer/catalog").status_code == 401
    assert c.get("/peer/catalog", headers={"Authorization": "Bearer wrong"}).status_code == 403


def test_catalog_served_to_paired_peer_only(client, tmp_path):
    c, config = client
    with db.connect(config.db_path) as conn:
        db.complete_pairing_as_acceptor(conn, "peer-c", "machine-c", "10.0.0.3", 8421, outgoing_token="o", incoming_token="valid-token")

        root = make_root(tmp_path, label="primary", role="primary", mode="mirror")
        write_file(root.path, "Movie.mkv", b"content")
        db.upsert_root(conn, root.label, str(root.path), root.role, root.mode)
        scanner.scan_root(conn, root, hash_algo="sha256")

    r = c.get("/peer/catalog", headers={"Authorization": "Bearer valid-token"})
    assert r.status_code == 200
    data = r.json()
    assert data["machine"] == "test-machine"
    assert data["files"][0]["rel_path"] == "Movie.mkv"
