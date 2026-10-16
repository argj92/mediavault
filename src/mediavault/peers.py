"""Orchestrates the LAN-peer workflow from the main (localhost-only) app:
initiating/accepting a pairing handshake, and fetching a paired peer's
catalog. All outbound calls (this machine acting as a client toward
another's peer_api.py) live here; peer_api.py only handles inbound requests.
"""
from __future__ import annotations

import secrets
import sqlite3

import requests

from . import catalog, db, pairing
from .config import AppConfig
from .discovery import PeerInfo

REQUEST_TIMEOUT = 5


def _gen_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _gen_token() -> str:
    return secrets.token_hex(16)


class PairingError(Exception):
    """A pairing step that can't proceed, for a reason worth telling the user
    in plain words (as opposed to a bug)."""


def require_lan_enabled(config: AppConfig) -> None:
    """Pairing needs this machine's peer API listening (the other side calls
    back to it), which only runs when the user opted in. Enforced here, not
    just hidden in the UI, so the CLI and hand-built requests obey it too."""
    if not config.enable_lan_discovery:
        raise PairingError(
            "Peer pairing is off on this machine. Set `enable_lan_discovery: true` in config.yaml and restart."
        )


def initiate_pairing(conn: sqlite3.Connection, config: AppConfig, peer: PeerInfo) -> str:
    """We ask `peer` to pair with us. Returns the pairing code to display —
    the user should confirm it matches what shows up on the peer's own
    screen before accepting there.

    The request goes out *before* anything is written: if the peer is
    unreachable or refuses, no half-made "pairing in progress" row is left
    behind (callers commit on a normal return, even one that handled the
    error)."""
    require_lan_enabled(config)
    existing = db.get_known_peer(conn, peer.peer_id)
    if existing is not None and existing["paired"]:
        raise PairingError(f"Already paired with {existing['machine_name']}. Remove it first to pair again.")

    own_peer_id = db.get_or_create_peer_id(conn)
    code = _gen_code()
    token = _gen_token()  # what THEY will present when calling US -- our own incoming_token

    resp = requests.post(
        f"http://{peer.address}:{peer.port}/peer/pair-request",
        json={
            "peer_id": own_peer_id,
            "machine_name": config.machine,
            "port": config.peer_port,
            "code": code,
            "token": token,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    db.upsert_pairing_peer(conn, peer.peer_id, peer.machine_name, peer.address, peer.port, incoming_token=token, paired=False)
    return code


def accept_pending_pairing(conn: sqlite3.Connection, config: AppConfig, peer_id: str) -> None:
    """The user confirmed a pending incoming request (code matched) —
    finish pairing and tell the initiator our token.

    The initiator is told first and our side is only recorded once it has
    accepted: the other order left us "paired" with a peer that never
    learned our token whenever the callback failed."""
    require_lan_enabled(config)
    db.purge_stale_pairings(conn, pairing.PENDING_TTL_SECONDS)
    pending = db.get_pending_pairing(conn, peer_id)
    if pending is None:
        raise PairingError("That pairing request has expired or was already handled; ask them to send it again.")

    own_peer_id = db.get_or_create_peer_id(conn)
    our_token = _gen_token()  # what WE want THEM to present when calling US
    resp = requests.post(
        f"http://{pending['address']}:{pending['port']}/peer/pair-confirm",
        json={
            "peer_id": own_peer_id,
            "token": our_token,
            "proof": pairing.pairing_proof(pending["outgoing_token"], own_peer_id, our_token),
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()

    db.complete_pairing_as_acceptor(
        conn, peer_id, pending["machine_name"], pending["address"], pending["port"],
        outgoing_token=pending["outgoing_token"], incoming_token=our_token,
    )
    db.remove_pending_pairing(conn, peer_id)


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
