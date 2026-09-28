"""Orchestrates the LAN-peer workflow from the main (localhost-only) app:
initiating/accepting a pairing handshake, and fetching a paired peer's
catalog. All outbound calls (this machine acting as a client toward
another's peer_api.py) live here; peer_api.py only handles inbound requests.
"""
from __future__ import annotations

import secrets
import sqlite3

import requests

from . import catalog, db
from .config import AppConfig
from .discovery import PeerInfo

REQUEST_TIMEOUT = 5


def _gen_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _gen_token() -> str:
    return secrets.token_hex(16)


def initiate_pairing(conn: sqlite3.Connection, config: AppConfig, peer: PeerInfo) -> str:
    """We ask `peer` to pair with us. Returns the pairing code to display —
    the user should confirm it matches what shows up on the peer's own
    screen before accepting there."""
    own_peer_id = db.get_or_create_peer_id(conn)
    code = _gen_code()
    token = _gen_token()  # what THEY will present when calling US -- our own incoming_token
    db.upsert_pairing_peer(conn, peer.peer_id, peer.machine_name, peer.address, peer.port, incoming_token=token, paired=False)

    resp = requests.post(
        f"http://{peer.address}:{peer.port}/peer/pair-request",
        json={
            "peer_id": own_peer_id,
            "machine_name": config.machine,
            "address": _own_address(),
            "port": config.peer_port,
            "code": code,
            "token": token,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return code


def accept_pending_pairing(conn: sqlite3.Connection, config: AppConfig, peer_id: str) -> None:
    """The user confirmed a pending incoming request (code matched) —
    finish pairing and tell the initiator our token."""
    pending = db.get_pending_pairing(conn, peer_id)
    if pending is None:
        raise ValueError(f"No pending pairing request for {peer_id}")

    own_peer_id = db.get_or_create_peer_id(conn)
    our_token = _gen_token()  # what WE want THEM to present when calling US
    db.complete_pairing_as_acceptor(
        conn, peer_id, pending["machine_name"], pending["address"], pending["port"],
        outgoing_token=pending["outgoing_token"], incoming_token=our_token,
    )
    db.remove_pending_pairing(conn, peer_id)

    resp = requests.post(
        f"http://{pending['address']}:{pending['port']}/peer/pair-confirm",
        json={"peer_id": own_peer_id, "token": our_token},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()


def reject_pending_pairing(conn: sqlite3.Connection, peer_id: str) -> None:
    db.remove_pending_pairing(conn, peer_id)


def sync_with_peer(conn: sqlite3.Connection, config: AppConfig, peer_row: sqlite3.Row) -> int:
    """Fetches a paired peer's catalog over the LAN and imports it —
    the automatic version of the manual export-a-file/AirDrop-it/import
    flow. Returns the number of files imported."""
    if not peer_row["paired"]:
        raise ValueError("Peer is not paired.")
    resp = requests.get(
        f"http://{peer_row['address']}:{peer_row['port']}/peer/catalog",
        headers={"Authorization": f"Bearer {peer_row['outgoing_token']}"},
        timeout=REQUEST_TIMEOUT * 4,  # a real catalog can be sizable JSON
    )
    resp.raise_for_status()
    data = resp.json()
    n = catalog.import_catalog(conn, data, config.machine)
    db.touch_peer_synced(conn, peer_row["peer_id"])
    return n


def _own_address() -> str:
    from .discovery import _local_ip

    return _local_ip()
