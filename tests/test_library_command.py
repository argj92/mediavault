"""`media-library`: reads back what the service was set up to do, controls it,
and explains in plain words why the dashboard might not be reachable."""
import json
import plistlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mediavault import library, service


# ----------------------------------------------------------------- addresses

def test_urls_for_loopback_and_tailscale():
    urls = library.resolve_urls(["127.0.0.1", "tailscale"], 8420, lambda: "100.72.77.6", lambda: "mini.tailnet.ts.net")
    assert urls == ["http://127.0.0.1:8420", "http://100.72.77.6:8420", "http://mini.tailnet.ts.net:8420"]


def test_a_tailscale_ip_in_the_plist_also_gets_its_name():
    urls = library.resolve_urls(["100.72.77.6"], 8420, lambda: None, lambda: "mini.tailnet.ts.net")
    assert urls == ["http://100.72.77.6:8420", "http://mini.tailnet.ts.net:8420"]


def test_tailscale_down_just_drops_its_addresses():
    assert library.resolve_urls(["127.0.0.1", "tailscale"], 8420, lambda: None, lambda: None) == ["http://127.0.0.1:8420"]


def test_wildcard_host_is_reached_via_loopback():
    assert library.resolve_urls(["0.0.0.0"], 9000) == ["http://127.0.0.1:9000"]


# ----------------------------------------------------------------- diagnosis

def test_not_installed():
    assert "isn't installed" in library.diagnose(False, None, [], {}, True)[0]


def test_installed_but_not_running():
    (p,) = library.diagnose(True, None, ["127.0.0.1"], {}, True)
    assert "not running" in p and "media-library start" in p


def test_the_live_situation_only_tailscale_so_localhost_refuses():
    problems = library.diagnose(True, 123, ["100.72.77.6"], {"http://100.72.77.6:8420": True}, True)
    assert len(problems) == 1 and "localhost" in problems[0] and "media-library install" in problems[0]


def test_running_but_nothing_answers():
    problems = library.diagnose(True, 123, ["127.0.0.1"], {"http://127.0.0.1:8420": False}, True)
    assert any("nothing answers" in p for p in problems) and any("127.0.0.1" in p for p in problems)


def test_tailscale_not_running_is_called_out():
    problems = library.diagnose(True, 1, ["127.0.0.1", "tailscale"], {"http://127.0.0.1:8420": True}, False)
    assert problems == ["Tailscale isn't running on this machine, so the tailnet address won't work until it is."]


def test_a_healthy_service_has_no_problems():
    ok = {"http://127.0.0.1:8420": True, "http://100.72.77.6:8420": True}
    assert library.diagnose(True, 1, ["127.0.0.1", "tailscale"], ok, True) == []


# --------------------------------------------------------------------- probe

@pytest.fixture()
def dashboard():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.path.startswith("/api/ui-state")
            self.send_response(200 if ok else 404)
            self.end_headers()
            self.wfile.write(json.dumps({"roots": [], "schedule": {}}).encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_probe_reads_the_dashboard_state(dashboard):
    assert library.probe(dashboard)["roots"] == []


def test_probe_is_none_when_nothing_listens():
    assert library.probe("http://127.0.0.1:9", timeout=0.5) is None


# ----------------------------------------------------- reading the definition

def test_definition_is_read_back_from_the_plist(tmp_path, monkeypatch):
    plist = tmp_path / "x.plist"
    plist.write_bytes(plistlib.dumps({
        "ProgramArguments": ["/v/bin/mediavault", "web", "--no-open-browser", "--host", "127.0.0.1", "--host", "tailscale", "--port", "9001"],
        "WorkingDirectory": "/proj", "EnvironmentVariables": {"MEDIAVAULT_CONFIG": "/proj/config.yaml"},
    }))
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service, "_plist_path", lambda: plist)

    assert service.definition() == {"bin": "/v/bin/mediavault", "hosts": ["127.0.0.1", "tailscale"], "port": 9001,
                                    "workdir": "/proj", "config": "/proj/config.yaml"}
    assert service.installed()


def test_definition_is_none_when_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service, "_plist_path", lambda: tmp_path / "missing.plist")
    assert service.definition() is None and not service.installed()


# ------------------------------------------------------------------- control

class Recorder:
    def __init__(self, loaded=True, pid_line="\tpid = 4242\n"):
        self.calls, self.loaded, self.pid_line = [], loaded, pid_line

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        is_print = cmd[:2] == ["launchctl", "print"]
        code = 0 if (not is_print or self.loaded) else 113
        return type("R", (), {"returncode": code, "stdout": f"state = running\n{self.pid_line}" if is_print else "", "stderr": ""})()


@pytest.fixture()
def mac(monkeypatch, tmp_path):
    plist = tmp_path / "a.plist"
    plist.write_text("x")
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service, "_plist_path", lambda: plist)
    monkeypatch.setattr(service, "_uid", lambda: 501)


def test_pid_is_parsed_from_launchctl(mac, monkeypatch):
    monkeypatch.setattr(service.subprocess, "run", Recorder())
    assert service.pid() == 4242


def test_pid_is_none_when_not_loaded_or_not_running(mac, monkeypatch):
    monkeypatch.setattr(service.subprocess, "run", Recorder(loaded=False))
    assert service.pid() is None
    monkeypatch.setattr(service.subprocess, "run", Recorder(pid_line=""))
    assert service.pid() is None


def test_start_bootstraps_when_unloaded_and_kickstarts_when_loaded(mac, monkeypatch):
    rec = Recorder(loaded=False)
    monkeypatch.setattr(service.subprocess, "run", rec)
    service.start()
    assert any(c[:3] == ["launchctl", "bootstrap", "gui/501"] for c in rec.calls)
    rec2 = Recorder(loaded=True)
    monkeypatch.setattr(service.subprocess, "run", rec2)
    service.start()
    assert ["launchctl", "kickstart", "gui/501/com.mediavault.agent"] in rec2.calls and not any(c[1] == "bootstrap" for c in rec2.calls)


def test_start_refuses_when_not_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service, "_plist_path", lambda: tmp_path / "none.plist")
    with pytest.raises(RuntimeError, match="media-library install"):
        service.start()


def test_stop_unloads_it_because_killing_would_be_undone_by_keepalive(mac, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(service.subprocess, "run", rec)
    service.stop()
    assert ["launchctl", "bootout", "gui/501/com.mediavault.agent"] in rec.calls


def test_restart_uses_kickstart_k(mac, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(service.subprocess, "run", rec)
    service.restart()
    assert ["launchctl", "kickstart", "-k", "gui/501/com.mediavault.agent"] in rec.calls


# ------------------------------------------------------------------------ CLI

def test_status_command_reports_the_one_problem_and_the_fix(monkeypatch, dashboard):
    monkeypatch.setattr(service, "installed", lambda: True)
    monkeypatch.setattr(service, "pid", lambda: 77)
    monkeypatch.setattr(service, "definition", lambda: {"hosts": ["100.72.77.6"], "port": 8420, "bin": None, "workdir": None, "config": None})
    monkeypatch.setattr(library, "resolve_urls", lambda hosts, port, *a: [dashboard])
    monkeypatch.setattr(library, "_tailscale_ip", lambda: "100.72.77.6")

    out = CliRunner().invoke(library.app, ["status"]).output

    assert "running" in out and "✓ answers" in out and "media-library install" in out


def test_bare_command_starts_a_stopped_service_then_reports_the_address(monkeypatch, dashboard):
    started = []
    state = {"pid": None}
    monkeypatch.setattr(service, "installed", lambda: True)
    monkeypatch.setattr(service, "pid", lambda: state["pid"])
    monkeypatch.setattr(service, "start", lambda: started.append(1) or state.update(pid=5))
    monkeypatch.setattr(service, "definition", lambda: {"hosts": ["127.0.0.1"], "port": 8420, "bin": None, "workdir": None, "config": None})
    monkeypatch.setattr(library, "resolve_urls", lambda hosts, port, *a: [dashboard])
    opened = []
    monkeypatch.setattr(library.webbrowser, "open", lambda u: opened.append(u))

    result = CliRunner().invoke(library.app, [])

    assert result.exit_code == 0 and started == [1] and opened == [dashboard]
    assert "MediaVault is up" in result.output


def test_bare_command_when_already_running_just_opens_it(monkeypatch, dashboard):
    monkeypatch.setattr(service, "installed", lambda: True)
    monkeypatch.setattr(service, "pid", lambda: 9)
    monkeypatch.setattr(service, "start", lambda: pytest.fail("already running"))
    monkeypatch.setattr(service, "definition", lambda: {"hosts": ["127.0.0.1"], "port": 8420, "bin": None, "workdir": None, "config": None})
    monkeypatch.setattr(library, "resolve_urls", lambda hosts, port, *a: [dashboard])
    opened = []
    monkeypatch.setattr(library.webbrowser, "open", lambda u: opened.append(u))
    result = CliRunner().invoke(library.app, ["--no-open"])
    assert "Already running" in result.output and opened == []


def test_shortcut_links_into_local_bin(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "venv" / "bin" / "media-library"
    fake_bin.parent.mkdir(parents=True)
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(library.sys, "argv", [str(fake_bin)])

    result = CliRunner().invoke(library.app, ["shortcut"])

    link = home / ".local" / "bin" / "media-library"
    assert link.is_symlink() and link.resolve() == fake_bin.resolve() and "PATH" in result.output
