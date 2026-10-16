"""Where the web UI listens.

One process can listen on several addresses at once -- typically loopback
*and* the Tailscale IP -- so the UI works at http://127.0.0.1:8420 on this
machine and over the tailnet. (Before this, `--host` took exactly one
address: serving the Tailscale IP meant localhost stopped working, and
keeping localhost meant running a second, redundant mediavault process --
which then scanned the same library twice.)

`--host tailscale` means "this machine's Tailscale IPv4 address", resolved at
startup, so a service definition doesn't hard-code an address that can change.
"""
from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import time

log = logging.getLogger("mediavault.web")


def tailscale_ip(wait_seconds: float = 0.0, interval: float = 2.0) -> str | None:
    """This machine's Tailscale IPv4, waiting up to `wait_seconds` for
    Tailscale to come up (a login-time service often starts first)."""
    exe = shutil.which("tailscale")
    if not exe:
        return None
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.split()
            if out:
                return out[0]
        except (OSError, subprocess.SubprocessError):
            pass
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def resolve_hosts(hosts: list[str], tailscale_wait: float = 30.0) -> list[str]:
    """Expand the 'tailscale' keyword and drop duplicates. A 'tailscale' that
    can't be resolved is skipped with a warning (the other addresses still
    serve) rather than failing startup."""
    resolved: list[str] = []
    for host in hosts:
        if host.lower() == "tailscale":
            ip = tailscale_ip(wait_seconds=tailscale_wait)
            if ip is None:
                log.warning("--host tailscale: Tailscale isn't installed or isn't up; not listening on it")
                continue
            host = ip
        if host not in resolved:
            resolved.append(host)
    return resolved


def bind_sockets(hosts: list[str], port: int) -> list[socket.socket]:
    """Bind each address. An address that can't be bound (e.g. the Tailscale
    IP before Tailscale is up) is skipped with a warning as long as at least
    one other works -- localhost staying reachable beats the whole service
    crash-looping over the tailnet one."""
    socks: list[socket.socket] = []
    errors: list[str] = []
    for host in hosts:
        sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.set_inheritable(True)
        except OSError as exc:
            sock.close()
            errors.append(f"{host}:{port}: {exc.strerror or exc}")
            log.warning("could not listen on %s:%s: %s", host, port, exc.strerror or exc)
            continue
        socks.append(sock)
    if not socks:
        raise OSError("; ".join(errors) or "no addresses to listen on")
    return socks


def make_server(app, hosts: list[str], port: int, log_level: str = "info"):
    """(uvicorn.Server, bound sockets). Split from serve() so a caller (or a
    test) can run it in a thread and stop it via server.should_exit."""
    import uvicorn

    sockets = bind_sockets(resolve_hosts(hosts), port)
    return uvicorn.Server(uvicorn.Config(app, log_level=log_level)), sockets


def serve(app, hosts: list[str], port: int, log_level: str = "info") -> None:
    server, sockets = make_server(app, hosts, port, log_level)
    server.run(sockets=sockets)
