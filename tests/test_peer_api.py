import pytest
from fastapi.testclient import TestClient

from mediavault import db, peer_api, scanner
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


def test_pair_request_rejects_missing_fields(client):
    c, _ = client
    r = c.post("/peer/pair-request", json={"peer_id": "abc"})
    assert r.status_code == 400


def test_pair_request_stores_pending(client):
    c, config = client
    r = c.post(
        "/peer/pair-request",
        json={"peer_id": "peer-a", "machine_name": "machine-a", "address": "10.0.0.1", "port": 8421, "code": "123456", "token": "tok-a"},
    )
    assert r.status_code == 200
    assert r.json() == {"status": "pending"}
    with db.connect(config.db_path) as conn:
        pending = db.list_pending_pairings(conn)
    assert len(pending) == 1
    assert pending[0]["code"] == "123456"


def test_pair_confirm_requires_pairing_in_progress(client):
    c, _ = client
    r = c.post("/peer/pair-confirm", json={"peer_id": "never-initiated", "token": "whatever"})
    assert r.status_code == 404


def test_pair_confirm_finalizes_initiated_pairing(client):
    c, config = client
    with db.connect(config.db_path) as conn:
        db.upsert_pairing_peer(conn, "peer-b", "machine-b", "10.0.0.2", 8421, incoming_token="our-token", paired=False)

    r = c.post("/peer/pair-confirm", json={"peer_id": "peer-b", "token": "their-token"})
    assert r.status_code == 200
    assert r.json() == {"status": "paired"}

    with db.connect(config.db_path) as conn:
        row = db.get_known_peer(conn, "peer-b")
    assert row["paired"] == 1
    assert row["outgoing_token"] == "their-token"
    assert row["incoming_token"] == "our-token"


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
