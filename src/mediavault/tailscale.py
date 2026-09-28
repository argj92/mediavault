"""Tailscale as a second, cross-network discovery source alongside mDNS
(discovery.py). mDNS only finds peers on the same LAN broadcast domain;
a tailnet reaches a peer wherever it is, already encrypted and
device-authenticated by Tailscale itself.

Being on the same tailnet is NOT treated as trust on its own, though -- a
tailnet can have more than one person's devices on it. This module only
answers "is there a mediavault peer API reachable at this tailnet address,
and what's its identity" by actually probing /peer/identity; the same
explicit mutual-pairing handshake (peers.py) is still required before any
data is ever exchanged, exactly as with an mDNS-discovered peer.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import requests

from .discovery import PeerInfo


def is_available() -> bool:
    return shutil.which("tailscale") is not None


def list_tailnet_peers() -> list[dict]:
    """Other devices on this tailnet (not mediavault peers specifically --
    just Tailscale's own device list). Empty if tailscale isn't installed,
    isn't logged in, or the daemon isn't running -- never raises."""
    if not is_available():
        return []
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    peers = []
    for p in (data.get("Peer") or {}).values():
        ips = p.get("TailscaleIPs") or []
        ipv4 = next((ip for ip in ips if "." in ip), None)  # prefer IPv4
        if not ipv4:
            continue
        peers.append(
            {
                "hostname": p.get("HostName", "unknown"),
                "dns_name": (p.get("DNSName") or "").rstrip("."),
                "address": ipv4,
                "online": bool(p.get("Online")),
            }
        )
    return peers


def probe_peer_identity(address: str, port: int, timeout: float = 2.0) -> PeerInfo | None:
    """Is a mediavault peer API actually reachable at this address? Returns
    None (not an error) for anything short of a clean response -- offline,
    not running mediavault, firewalled, LAN discovery disabled there, etc.
    are all just "not a candidate," not failures worth surfacing."""
    try:
        resp = requests.get(f"http://{address}:{port}/peer/identity", timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return PeerInfo(peer_id=data["peer_id"], machine_name=data["machine_name"], address=address, port=port)
    except (requests.RequestException, KeyError, ValueError):
        return None


def discover_tailnet_candidates(peer_port: int) -> list[PeerInfo]:
    """Every online tailnet peer that's actually running a reachable
    mediavault peer API right now. One probe per online device -- fine at
    the scale of a personal tailnet (a handful of devices), each bounded by
    probe_peer_identity's own short timeout."""
    candidates = []
    for peer in list_tailnet_peers():
        if not peer["online"]:
            continue
        found = probe_peer_identity(peer["address"], peer_port)
        if found is not None:
            candidates.append(found)
    return candidates
