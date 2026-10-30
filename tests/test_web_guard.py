"""The UI has no login and 26 state-changing routes, and is served on loopback
*and* the Tailscale interface. Without this guard a web page open in the
browser can drive it: CSRF (cross-site form POST to 127.0.0.1) and DNS
rebinding (attacker's domain re-pointed at 127.0.0.1, Host stays the
attacker's name)."""
import pytest
from fastapi.testclient import TestClient

from mediavault import db
from mediavault.config import AppConfig
from mediavault.web import app as web_app
from mediavault.web import guard


@pytest.fixture()
def env(tmp_path, monkeypatch):
    config = AppConfig(
        machine="Test-Machine.local", db_path=tmp_path / "mediavault.db", hash_algo="sha256",
        tmdb_api_key_env="X", video_extensions=[".mkv"], scan_interval_minutes=15,
        allowed_hosts=["vault.example.lan"],
    )
    with db.connect(config.db_path) as conn:
        db.init_db(conn)
    web_app.configure(config)
    monkeypatch.setattr(guard, "tailscale_dns_name", lambda: "mini.tailnet.ts.net")
    # No `with TestClient(...)`: that would start the real background scanner.
    return TestClient(web_app.app, base_url="http://127.0.0.1:8420"), config


def _ignored(config):
    with db.connect(config.db_path) as conn:
        return db.ignored_promotion_paths(conn)


@pytest.mark.parametrize("host", [
    "127.0.0.1:8420", "localhost:8420", "[::1]:8420", "100.72.77.6:8420", "192.168.1.5",
    "test-machine.local:8420", "mini.tailnet.ts.net:8420", "vault.example.lan",
    "Localhost.",          # case / trailing dot
    "akashs-mac-mini.taildc3029.ts.net:8420",   # our MagicDNS name even when the CLI can't report it
])
def test_legitimate_hosts_are_served(env, host):
    c, _ = env
    assert c.get("/api/status", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("host", [
    "evil.example", "evil.example:8420", "127.0.0.1.evil.example", "localhost.evil.example",
    "evilts.net", "mini.tailnet.ts.net.evil.example", "ts.net.evil.example",   # lookalikes of the Tailscale suffix
])
def test_unknown_hostnames_are_refused_dns_rebinding(env, host):
    c, _ = env
    r = c.get("/api/status", headers={"host": host})
    assert r.status_code == 400
    assert "allowed_hosts" in r.text  # tells a legitimate user how to fix it


def test_same_origin_post_works(env):
    c, config = env
    r = c.post("/promote/ignore", data={"rel_path": "a.mkv"}, headers={"origin": "http://127.0.0.1:8420"}, follow_redirects=False)
    assert r.status_code == 303
    assert _ignored(config) == {"a.mkv"}


@pytest.mark.parametrize("origin", [
    "http://evil.example",              # the classic cross-site form post
    "null",                             # sandboxed iframe / some redirects
    "http://127.0.0.1:3000",            # another local dev server on a different port
    "http://127.0.0.1",                 # same host, different (default) port
])
def test_cross_origin_post_is_blocked_and_changes_nothing(env, origin):
    c, config = env
    r = c.post("/promote/ignore", data={"rel_path": "b.mkv"}, headers={"origin": origin}, follow_redirects=False)
    assert r.status_code == 403
    assert _ignored(config) == set()


def test_post_without_origin_still_works_for_non_browser_clients(env):
    """curl / scripts send no Origin. A browser always does on a cross-site
    POST, so these can't be CSRF'd."""
    c, config = env
    r = c.post("/promote/ignore", data={"rel_path": "c.mkv"}, follow_redirects=False)
    assert r.status_code == 303
    assert _ignored(config) == {"c.mkv"}


def test_cross_site_fetch_metadata_without_origin_is_blocked(env):
    c, config = env
    r = c.post("/promote/ignore", data={"rel_path": "d.mkv"}, headers={"sec-fetch-site": "cross-site"}, follow_redirects=False)
    assert r.status_code == 403
    assert _ignored(config) == set()


def test_reads_are_not_subject_to_the_origin_check(env):
    c, _ = env
    assert c.get("/api/status", headers={"origin": "http://evil.example"}).status_code == 200


@pytest.mark.parametrize("value,expected", [
    ("Example.com:8420", ("example.com", "8420")),
    ("example.com.", ("example.com", "")),
    ("[::1]:8420", ("::1", "8420")),
    ("127.0.0.1", ("127.0.0.1", "")),
])
def test_split_host_header(value, expected):
    assert guard.split_host_header(value) == expected


def test_magicdns_name_is_allowed_even_when_the_tailscale_cli_cant_be_found(env, monkeypatch):
    """Under launchd PATH has no /opt/homebrew/bin, so asking the CLI for our
    name failed and the tailnet name was blocked. The .ts.net suffix covers it."""
    c, _ = env
    monkeypatch.setattr(guard, "tailscale_dns_name", lambda: None)
    assert c.get("/api/status", headers={"host": "akashs-mac-mini.taildc3029.ts.net:8420"}).status_code == 200


def test_find_tailscale_falls_back_when_path_is_minimal(monkeypatch, tmp_path):
    from mediavault import tailscale

    fake = tmp_path / "tailscale"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(tailscale.shutil, "which", lambda name: None)
    monkeypatch.setattr(tailscale, "_FALLBACK_PATHS", ("/nonexistent/tailscale", str(fake)))
    assert tailscale.find_tailscale() == str(fake) and tailscale.is_available()
    monkeypatch.setattr(tailscale, "_FALLBACK_PATHS", ("/nonexistent/tailscale",))
    assert tailscale.find_tailscale() is None and not tailscale.is_available()


def test_a_failed_tailscale_lookup_is_not_repeated_on_every_request(monkeypatch):
    """The guard runs per request; a Tailscale that's down must cost one slow
    subprocess per retry window, not one per page load."""
    calls = []
    monkeypatch.setattr(guard, "_ts_name", None)
    monkeypatch.setattr(guard, "_ts_failed_at", float("-inf"))
    monkeypatch.setattr(guard, "find_tailscale", lambda: "/x/tailscale")
    monkeypatch.setattr(guard.subprocess, "run", lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(OSError("down")))

    assert [guard.tailscale_dns_name() for _ in range(5)] == [None] * 5
    assert len(calls) == 1

    monkeypatch.setattr(guard, "_ts_failed_at", float("-inf"))      # retry window passed
    guard.tailscale_dns_name()
    assert len(calls) == 2
