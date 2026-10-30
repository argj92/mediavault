"""`media-library`: one command to get MediaVault up and to find out why it
isn't. Wraps the background service (service.py) with what a person actually
wants to ask -- "is it running, where do I open it, what's wrong, start it":

    media-library            make sure it's running, then open the dashboard
    media-library status     what's running, where it answers, scan times, problems
    media-library start|stop|restart
    media-library logs [-f]
    media-library install    set up the always-on service (loopback + Tailscale)
    media-library shortcut   put `media-library` on your PATH
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import typer
from rich.console import Console

from . import service

app = typer.Typer(help="Start MediaVault and see whether it's working.", invoke_without_command=True, add_completion=False)
console = Console()

DEFAULT_HOSTS = ["127.0.0.1", "tailscale"]


# ------------------------------------------------------------------ helpers

def resolve_urls(hosts: list[str], port: int, tailscale_ip=None, tailscale_name=None) -> list[str]:
    """Every address the dashboard should answer at, for the hosts it was
    installed with ('tailscale' stands for this machine's tailnet address)."""
    urls: list[str] = []
    for host in hosts:
        if host.lower() == "tailscale":
            ip = tailscale_ip() if tailscale_ip else None
            if ip:
                urls.append(f"http://{ip}:{port}")
                name = tailscale_name() if tailscale_name else None
                if name:
                    urls.append(f"http://{name}:{port}")
        elif host.startswith("100.") and _is_cgnat(host):
            urls.append(f"http://{host}:{port}")
            name = tailscale_name() if tailscale_name else None
            if name:
                urls.append(f"http://{name}:{port}")
        elif host in ("0.0.0.0", "::"):
            urls.append(f"http://127.0.0.1:{port}")
        else:
            h = f"[{host}]" if ":" in host else host
            urls.append(f"http://{h}:{port}")
    return list(dict.fromkeys(urls))


def _is_cgnat(host: str) -> bool:
    """100.64.0.0/10: the range Tailscale hands out."""
    try:
        a, b, *_ = (int(x) for x in host.split("."))
    except ValueError:
        return False
    return a == 100 and 64 <= b <= 127


def probe(url: str, timeout: float = 3.0) -> dict | None:
    """The dashboard's own state if it answers at `url`, else None."""
    try:
        with urllib.request.urlopen(f"{url}/api/ui-state?counts=0", timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode())
    except (OSError, ValueError, urllib.error.URLError):
        pass
    return None


def diagnose(installed: bool, pid: int | None, hosts: list[str], reachable: dict[str, bool], tailscale_up: bool | None) -> list[str]:
    """Plain-words problems and what to do about each. Pure, so it can be
    tested without a machine in any particular state."""
    if not installed:
        return ["The background service isn't installed. Run `media-library install`."]
    problems: list[str] = []
    if pid is None:
        problems.append("The service is installed but not running. Run `media-library start`.")
        return problems
    if not any(reachable.values()):
        problems.append("The service is running but nothing answers at its addresses. Check `media-library logs`.")
    local = [u for u in reachable if "127.0.0.1" in u or "localhost" in u]
    if "127.0.0.1" not in hosts and "localhost" not in hosts and "0.0.0.0" not in hosts:
        problems.append(
            "It isn't listening on this machine's own address (127.0.0.1), only on the Tailscale one, "
            "so http://localhost:8420 will refuse the connection. Run `media-library install` to serve both."
        )
    elif local and not any(ok for u, ok in reachable.items() if u in local):
        problems.append("It should be listening on 127.0.0.1 but isn't answering there. Run `media-library restart`.")
    if any(h.lower() == "tailscale" or _is_cgnat(h) for h in hosts) and tailscale_up is False:
        problems.append("Tailscale isn't running on this machine, so the tailnet address won't work until it is.")
    return problems


def _tailscale_ip():
    from .web.serve import tailscale_ip

    return tailscale_ip(wait_seconds=0)


def _tailscale_name():
    from .web.guard import tailscale_dns_name

    return tailscale_dns_name()


def _addresses() -> tuple[list[str], list[str], int]:
    d = service.definition()
    hosts = d["hosts"] if d else list(DEFAULT_HOSTS)
    port = d["port"] if d else 8420
    return hosts, resolve_urls(hosts, port, _tailscale_ip, _tailscale_name), port


def _wait_until_up(urls: list[str], seconds: float = 25.0) -> str | None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for url in urls:
            if probe(url, timeout=2.0):
                return url
        time.sleep(1.0)
    return None


def _ago(seconds: float) -> str:
    seconds = max(0, seconds)
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    return f"{seconds / 3600:.1f} h ago"


def _in(seconds: float) -> str:
    if seconds < 90:
        return "any moment"
    if seconds < 5400:
        return f"in {round(seconds / 60)} min"
    return f"in {seconds / 3600:.1f} h"


# ----------------------------------------------------------------- commands

@app.callback()
def main(ctx: typer.Context, open_browser: bool = typer.Option(True, "--open/--no-open", help="open the dashboard in your browser")):
    """With no subcommand: make sure MediaVault is running, then open it."""
    if ctx.invoked_subcommand is not None:
        return
    up(open_browser)


def up(open_browser: bool = True) -> None:
    if not service.installed():
        console.print("MediaVault isn't set up as a background service yet.")
        if not typer.confirm("Install it now (starts at login, keeps running)?", default=True):
            raise typer.Exit(1)
        install()
    elif service.pid() is None:
        console.print("It isn't running — starting it…")
        try:
            service.start()
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
    else:
        console.print("Already running.")

    hosts, urls, _ = _addresses()
    working = _wait_until_up(urls)
    if working is None:
        console.print("[red]Started, but it isn't answering yet.[/red] Check `media-library status` and `media-library logs`.")
        raise typer.Exit(1)
    console.print(f"[green]MediaVault is up:[/green] {working}")
    others = [u for u in urls if u != working]
    if others:
        console.print("Also at: " + "  ".join(others))
    if open_browser:
        webbrowser.open(working)


@app.command()
def status():
    """What's running, where it answers, when it last scanned, and what's wrong."""
    hosts, urls, _ = _addresses()
    pid = service.pid()
    console.print(f"Service:   {'[green]running[/green] (pid ' + str(pid) + ')' if pid else '[red]not running[/red]'}"
                  f"{'' if service.installed() else '  (not installed)'}")
    reachable: dict[str, bool] = {}
    state = None
    for url in urls:
        got = probe(url)
        reachable[url] = got is not None
        state = state or got
        console.print(f"Address:   {url}  {'[green]✓ answers[/green]' if got else '[red]✗ no answer[/red]'}")
    if state:
        sched = state.get("schedule", {})
        now = time.time()
        if sched.get("running"):
            console.print("Scanning:  [yellow]a scan is running now[/yellow]")
        if sched.get("last_finished_at"):
            took = f", took {round(sched['last_duration'])}s" if sched.get("last_duration") is not None else ""
            console.print(f"Last scan: {_ago(now - sched['last_finished_at'])}{took}")
        if sched.get("next_run_at") and not sched.get("running"):
            console.print(f"Next scan: {_in(sched['next_run_at'] - now)}  (every {sched.get('interval_label', '?')})")
        for r in state.get("roots", []):
            mark = {"ok": "[green]✓[/green]", "scanning": "[yellow]…[/yellow]"}.get(r["state"], "[red]✗[/red]")
            console.print(f"  {mark} {r['label']}: {r['text']}")
    problems = diagnose(service.installed(), pid, hosts, reachable, _tailscale_ip() is not None)
    if problems:
        console.print("\n[bold]Problems:[/bold]")
        for p in problems:
            console.print(f"  • {p}")
    else:
        console.print("\n[green]Nothing wrong.[/green]")


@app.command()
def start():
    """Start the background service."""
    try:
        console.print(service.start())
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


@app.command()
def stop():
    """Stop it (until `start` or the next login)."""
    console.print(service.stop())


@app.command()
def restart():
    """Restart it (e.g. after updating the code)."""
    try:
        console.print(service.restart())
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


@app.command()
def logs(follow: bool = typer.Option(False, "-f", "--follow", help="keep printing new lines"), lines: int = 60):
    """Show the service's recent log (errors and scan notes)."""
    path = service.log_paths()["err"]
    if not path.exists():
        console.print(f"No log yet at {path}")
        raise typer.Exit(1)
    os.execvp("tail", ["tail", "-n", str(lines)] + (["-f"] if follow else []) + [str(path)])


@app.command()
def install(
    port: int = typer.Option(8420, help="port"),
    host: list[str] = typer.Option(DEFAULT_HOSTS, "--host", help="address(es) to serve; 'tailscale' means this machine's tailnet address"),
):
    """Set up the always-on service, serving this machine and your tailnet."""
    d = service.definition()
    bin_path = Path(d["bin"]) if d and d.get("bin") else Path(sys.executable).parent / "mediavault"
    if not bin_path.exists():
        console.print(f"[red]Couldn't find the mediavault program at {bin_path}[/red]")
        raise typer.Exit(1)
    workdir = Path(d["workdir"]) if d and d.get("workdir") else bin_path.resolve().parents[2]
    if d and d.get("config"):
        config_path = Path(d["config"])
    else:
        from .config import ConfigError, load_config

        try:
            config_path = load_config().config_path.resolve()
        except ConfigError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
    msg = service.install(bin_path.resolve() if not d else bin_path, workdir, config_path, port=port, hosts=host)
    console.print(f"[green]{msg}[/green]")


@app.command()
def shortcut():
    """Put `media-library` on your PATH (a link in ~/.local/bin)."""
    target = Path(sys.argv[0]).resolve()
    if target.name != "media-library":
        target = Path(sys.executable).parent / "media-library"
    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    link = bin_dir / "media-library"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)
    console.print(f"[green]Linked {link} → {target}[/green]")
    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        console.print(f"{bin_dir} isn't on your PATH yet. Add this line to ~/.zshrc, then open a new terminal:\n"
                      f'  export PATH="$HOME/.local/bin:$PATH"')


def main_entry() -> None:
    app()
