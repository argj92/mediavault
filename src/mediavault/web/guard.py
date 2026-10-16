"""Request guard for the management UI.

The UI has no login and 26 state-changing routes (quarantine, purge the
recycle bin, apply deletions, sync, remove roots...). That was tolerable when
it only listened on 127.0.0.1 for one person, but it's now also served on the
Tailscale interface -- and even on loopback, *a web page open in the browser
can reach it*:

- **CSRF**: any site can make the browser POST a form to
  http://127.0.0.1:8420/recycle-bin/empty (or the Tailscale address). The
  browser attaches no credentials, so nothing needs to be stolen -- the
  request just works. Browsers always send an `Origin` header on a
  cross-site POST, so requiring it to match the server's own address stops
  this.
- **DNS rebinding**: an attacker's domain flips its DNS answer to 127.0.0.1;
  the page is then "same-origin" with the local UI and can read and drive it
  freely. The browser still sends `Host: attacker.example`, so only answering
  to names that are really ours stops this.

This isn't authentication: anyone who can reach the port with a legitimate
Host and no browser (curl on the tailnet) still gets in -- that's what the
Tailscale ACLs are for. It closes the "a webpage you visit can drive your
backups" hole, which no ACL can.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import subprocess
from urllib.parse import urlsplit

from ..tailscale import find_tailscale

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_HOW_TO_FIX = (
    "If this is a name you really use to reach MediaVault, add it to `allowed_hosts` in config.yaml."
)


def split_host_header(value: str) -> tuple[str, str]:
    """'Example.com:8420' -> ('example.com', '8420'); '[::1]:8420' -> ('::1', '8420')."""
    value = value.strip().lower()
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        return host, rest.lstrip(":")
    host, _, port = value.partition(":")
    return host.rstrip("."), port


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


_ts_name: str | None = None


def tailscale_dns_name() -> str | None:
    """This machine's MagicDNS name (e.g. 'mac-mini.tailnet.ts.net'), if
    Tailscale is installed and up. Best-effort: None on any failure. Only a
    success is remembered -- a failure (Tailscale not up yet at login) is
    retried rather than blocking the name for the life of the process."""
    global _ts_name
    if _ts_name:
        return _ts_name
    exe = find_tailscale()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=3).stdout
        name = json.loads(out).get("Self", {}).get("DNSName", "")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    _ts_name = name.rstrip(".").lower() or None
    return _ts_name


# Tailscale's own domain for MagicDNS names. Only Tailscale can publish records
# under it, and a name there resolves to a tailnet address -- never to this
# machine's loopback -- so it can't be used for DNS rebinding the way an
# attacker-controlled domain can. Accepting the suffix means the UI works over
# the tailnet even when the CLI can't be asked for our exact name.
TAILSCALE_SUFFIX = ".ts.net"


def allowed_names(config) -> set[str]:
    """Hostnames (not IPs -- those are always fine, see host_ok) this server
    answers to."""
    names = {"localhost", socket.gethostname().lower(), str(config.machine).lower()}
    names |= {str(h).lower().rstrip(".") for h in getattr(config, "allowed_hosts", [])}
    ts = tailscale_dns_name()
    if ts:
        names.add(ts)
    return names


def host_ok(hostname: str, names: set[str]) -> bool:
    # An IP literal can't be a DNS-rebinding vector (rebinding needs a name).
    return _is_ip(hostname) or hostname in names or hostname.endswith(TAILSCALE_SUFFIX)


class RequestGuard:
    """Pure-ASGI middleware (no BaseHTTPMiddleware: that buffers streaming
    responses, and /play streams video)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        config = getattr(scope["app"].state, "config", None)
        host_header = headers.get("host", "")
        hostname, _ = split_host_header(host_header)

        if config is not None and not host_ok(hostname, allowed_names(config)):
            return await _reject(send, 400, f"Blocked: unexpected Host '{host_header}'. {_HOW_TO_FIX}")

        if scope["method"] in UNSAFE_METHODS:
            origin = headers.get("origin")
            if origin is not None:
                # Compared on host:port only, not scheme: nobody but us can serve
                # a page from our own host:port, and requiring "http" would break
                # access through a TLS-terminating proxy (`tailscale serve`).
                # `Origin: null` is what sandboxed frames / some redirects send.
                if origin == "null" or urlsplit(origin).netloc.lower() != host_header.strip().lower():
                    return await _reject(send, 403, f"Blocked: cross-origin request from '{origin}'.")
            elif headers.get("sec-fetch-site") == "cross-site":
                return await _reject(send, 403, "Blocked: cross-site request.")

        await self.app(scope, receive, send)


async def _reject(send, status: int, message: str) -> None:
    body = (message + "\n").encode()
    await send({
        "type": "http.response.start", "status": status,
        "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})
