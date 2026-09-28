"""Finds other mediavault instances on the local network via mDNS/Bonjour
(the same mechanism AirPlay/printers/Chromecast use) so pairing doesn't
require typing in IP addresses. Advertising or browsing is what triggers
macOS's native "Local Network" permission prompt the first time -- that's a
real OS-level consent dialog, not something this module can pre-approve.

Purely opt-in: nothing in this module runs unless config.enable_lan_discovery
is true (see cli.py's service wiring). Discovery only tells you a peer
*exists*; it grants it no trust or access on its own -- see peer_api.py for
the separate, explicit mutual-pairing step before any data is ever shared.
"""
from __future__ import annotations

import logging
import socket
import threading
from dataclasses import dataclass

from zeroconf import ServiceInfo, ServiceStateChange, Zeroconf
from zeroconf import ServiceBrowser as _ServiceBrowser

SERVICE_TYPE = "_mediavault._tcp.local."
log = logging.getLogger("mediavault.discovery")


@dataclass
class PeerInfo:
    peer_id: str
    machine_name: str
    address: str
    port: int


def _local_ip() -> str:
    """Best-effort LAN-facing IP (no packets actually sent -- UDP connect()
    to a public address just makes the OS pick the right outbound
    interface). Falls back to loopback if genuinely offline."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def make_service_info(machine_name: str, peer_id: str, port: int) -> ServiceInfo:
    ip = _local_ip()
    return ServiceInfo(
        SERVICE_TYPE,
        f"{machine_name}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(ip)],
        port=port,
        properties={"peer_id": peer_id},
        server=f"{machine_name}.local.",
    )


class Advertiser:
    """Announces this machine's mediavault instance on the LAN. Nothing more
    than "I exist, here's my name/peer_id/port" -- no catalog data, no trust."""

    def __init__(self, zc: Zeroconf, machine_name: str, peer_id: str, port: int):
        self._zc = zc
        self._info = make_service_info(machine_name, peer_id, port)

    def start(self) -> None:
        self._zc.register_service(self._info)

    def stop(self) -> None:
        try:
            self._zc.unregister_service(self._info)
        except Exception:  # noqa: BLE001 — best-effort on shutdown
            pass


class Browser:
    """Maintains a live, in-memory set of other mediavault instances
    currently visible on the LAN. Ephemeral by design -- restart and it's
    empty until peers announce themselves again; the *trusted* peer list
    (db.known_peers) is what persists."""

    def __init__(self, zc: Zeroconf, own_peer_id: str):
        self._zc = zc
        self._own_peer_id = own_peer_id
        self._lock = threading.Lock()
        self._peers: dict[str, PeerInfo] = {}
        self._browser: _ServiceBrowser | None = None

    def _on_change(self, zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange) -> None:
        from zeroconf import ServiceStateChange as SSC

        if state_change is SSC.Removed:
            with self._lock:
                self._peers = {pid: p for pid, p in self._peers.items() if f"{p.machine_name}.{SERVICE_TYPE}" != name}
            return

        info = zeroconf.get_service_info(service_type, name, timeout=2000)
        if info is None or not info.properties:
            return
        peer_id_bytes = info.properties.get(b"peer_id")
        if not peer_id_bytes:
            return
        peer_id = peer_id_bytes.decode()
        if peer_id == self._own_peer_id:
            return  # don't discover ourselves
        addresses = info.parsed_addresses()
        if not addresses:
            return
        machine_name = name[: -len("." + SERVICE_TYPE)] if name.endswith("." + SERVICE_TYPE) else name
        with self._lock:
            self._peers[peer_id] = PeerInfo(peer_id=peer_id, machine_name=machine_name, address=addresses[0], port=info.port)

    def start(self) -> None:
        self._browser = _ServiceBrowser(self._zc, SERVICE_TYPE, handlers=[self._on_change])

    def stop(self) -> None:
        if self._browser is not None:
            self._browser.cancel()

    def list_peers(self) -> list[PeerInfo]:
        with self._lock:
            return list(self._peers.values())
