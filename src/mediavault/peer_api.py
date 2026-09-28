"""The ONLY part of mediavault that listens on the LAN interface (not just
127.0.0.1) — deliberately a small, separate app from the main web UI, not a
LAN-exposed mode of it. Four routes:

- GET  /peer/identity      -- public: who am I (peer_id/machine_name)
- POST /peer/pair-request  -- an incoming "someone wants to pair" handshake
- POST /peer/pair-confirm  -- an incoming "they accepted, here's my token"
- GET  /peer/catalog       -- requires a bearer token from a completed,
                               mutual pairing (see db.known_peers) -- the
                               only thing an unpaired caller can never get.

Nothing here can scan, sync, delete, or modify anything -- it's read-only
catalog data (hash/size/path/role, never file contents) gated behind mutual
pairing, plus the handshake needed to establish that pairing in the first
place. The main management UI (roots, sync, recycle bin, etc.) stays on the
existing 127.0.0.1-only app regardless of whether this is enabled.
"""
from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException, Request

from . import catalog, db

app = FastAPI(title="mediavault-peer")


def get_config(request: Request):
    return request.app.state.config


def get_conn(request: Request):
    return db.connect(get_config(request).db_path)


@app.get("/peer/identity")
def identity(request: Request):
    config = get_config(request)
    with get_conn(request) as conn:
        peer_id = db.get_or_create_peer_id(conn)
    return {"peer_id": peer_id, "machine_name": config.machine}


@app.post("/peer/pair-request")
def pair_request(request: Request, body: dict):
    """Someone (claiming to be `body['machine_name']`) wants to pair with
    us. Stored as pending -- nothing is trusted until a human confirms it
    matches the code shown on the initiating machine too."""
    required = {"peer_id", "machine_name", "address", "port", "code", "token"}
    if not required.issubset(body):
        raise HTTPException(400, f"Missing fields: {required - set(body)}")
    with get_conn(request) as conn:
        db.add_pending_pairing(
            conn, body["peer_id"], body["machine_name"], body["address"], int(body["port"]),
            body["code"], body["token"],
        )
    return {"status": "pending"}


@app.post("/peer/pair-confirm")
def pair_confirm(request: Request, body: dict):
    """The peer we sent a pair-request to has accepted and generated their
    own token for us to use. Only meaningful if we actually have a
    pairing-in-progress row for them (i.e. we really did initiate this)."""
    if "peer_id" not in body or "token" not in body:
        raise HTTPException(400, "Missing peer_id/token")
    with get_conn(request) as conn:
        existing = db.get_known_peer(conn, body["peer_id"])
        if existing is None or existing["paired"]:
            raise HTTPException(404, "No pairing in progress for this peer.")
        db.finalize_pairing(conn, body["peer_id"], body["token"])
    return {"status": "paired"}


@app.get("/peer/catalog")
def peer_catalog(request: Request, authorization: str = Header(default="")):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token.")
    token = authorization.removeprefix("Bearer ").strip()
    config = get_config(request)
    with get_conn(request) as conn:
        peer = db.find_peer_by_incoming_token(conn, token)
        if peer is None:
            raise HTTPException(403, "Not a paired peer.")
        data = catalog.build_catalog(conn, config.machine)
    return data
