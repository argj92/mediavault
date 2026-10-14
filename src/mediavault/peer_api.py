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
from pydantic import BaseModel, Field

from . import catalog, db, pairing

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


class PairRequest(BaseModel):
    """Everything here comes from an unauthenticated caller on the LAN, so it
    is shape-checked before it's stored or ever rendered in the UI. There is
    deliberately no `address`: where to call the requester back is the
    connection's source IP, not something it gets to claim."""
    peer_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    machine_name: str = Field(min_length=1, max_length=64, pattern=r"^[^\x00-\x1f\x7f]+$")
    port: int = Field(ge=1, le=65535)
    code: str = Field(pattern=r"^\d{6}$")
    token: str = Field(pattern=r"^[0-9a-f]{32}$")


class PairConfirm(BaseModel):
    peer_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    token: str = Field(pattern=r"^[0-9a-f]{32}$")
    proof: str = Field(pattern=r"^[0-9a-f]{64}$")


@app.post("/peer/pair-request")
def pair_request(request: Request, body: PairRequest):
    """Someone (claiming to be `body.machine_name`) wants to pair with us.
    Stored as pending -- nothing is trusted until a human confirms the code
    matches the one shown on the initiating machine too."""
    source = request.client.host if request.client else "unknown"
    with get_conn(request) as conn:
        if body.peer_id == db.get_or_create_peer_id(conn):
            raise HTTPException(400, "Can't pair with yourself.")
        db.purge_stale_pairings(conn, pairing.PENDING_TTL_SECONDS)

        known = db.get_known_peer(conn, body.peer_id)
        if known is not None and known["paired"]:
            # Re-keying an existing trust relationship must start from the
            # human removing it here -- never from a request.
            raise HTTPException(409, "Already paired; remove the pairing on this machine first.")

        pending = db.list_pending_pairings(conn)
        mine = next((p for p in pending if p["peer_id"] == body.peer_id), None)
        if mine is not None and mine["address"] != source:
            # First request for an id owns it until answered or expired, so
            # another host can't overwrite the code/token a human is about to confirm.
            raise HTTPException(409, "A pairing request for this peer is already waiting.")
        if mine is None:
            if len(pending) >= pairing.MAX_PENDING_TOTAL:
                raise HTTPException(429, "Too many pairing requests waiting; answer or reject some first.")
            if sum(1 for p in pending if p["address"] == source) >= pairing.MAX_PENDING_PER_SOURCE:
                raise HTTPException(429, "Too many pairing requests from this address.")
        db.add_pending_pairing(conn, body.peer_id, body.machine_name, source, body.port, body.code, body.token)
    return {"status": "pending"}


@app.post("/peer/pair-confirm")
def pair_confirm(request: Request, body: PairConfirm):
    """The peer we sent a pair-request to has accepted and generated their
    own token for us to use. Only meaningful if we actually have a
    pairing-in-progress row for them (we really did initiate this) *and* the
    caller can prove it received our request (see pairing.py)."""
    with get_conn(request) as conn:
        db.purge_stale_pairings(conn, pairing.PENDING_TTL_SECONDS)
        existing = db.get_known_peer(conn, body.peer_id)
        if existing is None or existing["paired"] or not existing["incoming_token"]:
            raise HTTPException(404, "No pairing in progress for this peer.")
        if not pairing.proof_ok(existing["incoming_token"], body.peer_id, body.token, body.proof):
            raise HTTPException(403, "Bad pairing proof.")
        if not db.finalize_pairing(conn, body.peer_id, body.token):
            raise HTTPException(404, "No pairing in progress for this peer.")
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
