"""Listening on several addresses (loopback + Tailscale) from one process."""
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mediavault import cli, service
from mediavault.config import AppConfig
from mediavault.web import serve


def test_resolve_hosts_expands_tailscale_and_dedupes(monkeypatch):
    monkeypatch.setattr(serve, "tailscale_ip", lambda wait_seconds=0: "100.64.0.9")
    assert serve.resolve_hosts(["127.0.0.1", "tailscale", "100.64.0.9", "127.0.0.1"]) == ["127.0.0.1", "100.64.0.9"]


def test_unresolvable_tailscale_is_skipped_not_fatal(monkeypatch):
    monkeypatch.setattr(serve, "tailscale_ip", lambda wait_seconds=0: None)
    assert serve.resolve_hosts(["127.0.0.1", "tailscale"]) == ["127.0.0.1"]


def test_an_unbindable_address_is_skipped_as_long_as_one_works():
    # 203.0.113.0/24 is reserved for documentation: never a local address.
    socks = serve.bind_sockets(["127.0.0.1", "203.0.113.1"], 0)
    try:
        assert len(socks) == 1 and socks[0].getsockname()[0] == "127.0.0.1"
    finally:
        [s.close() for s in socks]


def test_binding_nothing_is_an_error():
    with pytest.raises(OSError, match="203.0.113.1"):
        serve.bind_sockets(["203.0.113.1"], 0)


def _ipv6_loopback_available() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _ipv6_loopback_available(), reason="needs IPv6 loopback")
def test_one_process_answers_on_two_addresses():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server, sockets = serve.make_server(app, ["127.0.0.1", "::1"], port, log_level="warning")
    thread = threading.Thread(target=server.run, kwargs={"sockets": sockets}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3).read() == b"ok"
        assert urllib.request.urlopen(f"http://[::1]:{port}/", timeout=3).read() == b"ok"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_web_command_passes_every_host_through(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(serve, "serve", lambda app, hosts, port, log_level="info": seen.update(hosts=hosts, port=port))
    monkeypatch.setattr(cli, "_load", lambda *a, **k: AppConfig(
        machine="m", db_path=tmp_path / "m.db", hash_algo="sha256", tmdb_api_key_env="X",
        video_extensions=[".mkv"], scan_interval_minutes=15))

    result = CliRunner().invoke(
        cli.app, ["web", "--host", "127.0.0.1", "--host", "tailscale", "--port", "9000", "--no-open-browser"])

    assert result.exit_code == 0, result.output
    assert seen == {"hosts": ["127.0.0.1", "tailscale"], "port": 9000}


def test_web_command_default_is_still_loopback_only(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(serve, "serve", lambda app, hosts, port, log_level="info": seen.update(hosts=hosts))
    monkeypatch.setattr(cli, "_load", lambda *a, **k: AppConfig(
        machine="m", db_path=tmp_path / "m.db", hash_algo="sha256", tmdb_api_key_env="X",
        video_extensions=[".mkv"], scan_interval_minutes=15))

    assert CliRunner().invoke(cli.app, ["web", "--no-open-browser"]).exit_code == 0
    assert seen["hosts"] == ["127.0.0.1"]


def test_launchd_plist_carries_every_host(tmp_path, monkeypatch):
    plist = tmp_path / "agent.plist"
    monkeypatch.setattr(service, "_plist_path", lambda: plist)
    monkeypatch.setattr(service, "_log_dir", lambda: tmp_path)
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stderr": ""})())

    service.install(Path("/bin/mediavault"), Path("/proj"), Path("/cfg.yaml"), port=8420, hosts=["127.0.0.1", "tailscale"])

    args = [l.strip().removeprefix("<string>").removesuffix("</string>") for l in plist.read_text().splitlines() if "<string>" in l]
    i = args.index("--no-open-browser")
    assert args[i + 1:i + 7] == ["--host", "127.0.0.1", "--host", "tailscale", "--port", "8420"]


def test_a_single_host_string_still_works(tmp_path, monkeypatch):
    """Existing callers / re-running an old `service install --host X`."""
    plist = tmp_path / "agent.plist"
    monkeypatch.setattr(service, "_plist_path", lambda: plist)
    monkeypatch.setattr(service, "_log_dir", lambda: tmp_path)
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stderr": ""})())

    service.install(Path("/bin/mediavault"), Path("/proj"), Path("/cfg.yaml"), port=8420, hosts="100.72.77.6")

    text = plist.read_text()
    assert text.count("<string>--host</string>") == 1 and "<string>100.72.77.6</string>" in text


def test_systemd_unit_carries_every_host(tmp_path, monkeypatch):
    unit = tmp_path / "mediavault.service"
    monkeypatch.setattr(service, "_unit_path", lambda: unit)
    monkeypatch.setattr(service, "_log_dir", lambda: tmp_path)
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: None)

    service._install_linux(Path("/bin/mediavault"), Path("/proj"), Path("/cfg.yaml"), 8420, ["127.0.0.1", "tailscale"])

    assert "ExecStart=/bin/mediavault web --no-open-browser --host 127.0.0.1 --host tailscale --port 8420" in unit.read_text()
