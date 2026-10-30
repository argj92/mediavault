"""Installs mediavault as an always-on background service (so scanning,
duplicate detection, and sync-status notifications keep happening even when
no one has the web UI or desktop app open), using each OS's native service
manager: launchd on macOS, systemd --user on Linux.

The service just runs `mediavault web --no-open-browser` — the same web UI,
kept alive in the background. Uninstalling always restores things to
exactly how they were before (no leftover files, nothing left running).
"""
from __future__ import annotations

import getpass
import platform
import subprocess
from pathlib import Path

LABEL = "com.mediavault.agent"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "mediavault.service"


def _log_dir() -> Path:
    d = Path.home() / "Library" / "Logs" / "mediavault"
    if platform.system() != "Darwin":
        d = Path.home() / ".local" / "share" / "mediavault" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def install(bin_path: Path, project_dir: Path, config_path: Path, port: int = 8420, hosts: list[str] | str = "127.0.0.1") -> str:
    hosts = [hosts] if isinstance(hosts, str) else list(hosts)
    system = platform.system()
    if system == "Darwin":
        return _install_macos(bin_path, project_dir, config_path, port, hosts)
    if system == "Linux":
        return _install_linux(bin_path, project_dir, config_path, port, hosts)
    raise RuntimeError(
        f"No automatic background-service support for {system} yet. "
        "Run `mediavault web` (or `gui`) manually, or set up a Scheduled Task yourself."
    )


def uninstall() -> str:
    system = platform.system()
    if system == "Darwin":
        return _uninstall_macos()
    if system == "Linux":
        return _uninstall_linux()
    raise RuntimeError(f"No background-service support for {system}.")


def status() -> str:
    system = platform.system()
    if system == "Darwin":
        result = subprocess.run(["launchctl", "list", LABEL], capture_output=True, text=True)
        return result.stdout.strip() or result.stderr.strip() or "not installed"
    if system == "Linux":
        result = subprocess.run(
            ["systemctl", "--user", "status", "mediavault.service"], capture_output=True, text=True
        )
        return result.stdout.strip() or result.stderr.strip()
    return f"No background-service support for {system}."


# ------------------------------------------------- inspect and control

def log_paths() -> dict[str, Path]:
    logs = _log_dir()
    return {"out": logs / "service.log", "err": logs / "service.err.log"}


def installed() -> bool:
    system = platform.system()
    if system == "Darwin":
        return _plist_path().exists()
    if system == "Linux":
        return _unit_path().exists()
    return False


def definition() -> dict | None:
    """What the installed service was set up to do (macOS: read back from the
    plist): {bin, hosts, port, workdir, config}. None if not installed or
    unreadable. This is the source of truth for 'where should it be
    listening', rather than guessing."""
    if platform.system() != "Darwin" or not _plist_path().exists():
        return None
    import plistlib

    try:
        data = plistlib.loads(_plist_path().read_bytes())
    except (OSError, ValueError):
        return None
    args = [str(a) for a in data.get("ProgramArguments", [])]
    hosts = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--host"]
    port = next((int(args[i + 1]) for i, a in enumerate(args[:-1]) if a == "--port" and args[i + 1].isdigit()), 8420)
    return {
        "bin": args[0] if args else None,
        "hosts": hosts or ["127.0.0.1"],
        "port": port,
        "workdir": data.get("WorkingDirectory"),
        "config": (data.get("EnvironmentVariables") or {}).get("MEDIAVAULT_CONFIG"),
    }


def pid() -> int | None:
    """The running service's process id, or None if it isn't running."""
    system = platform.system()
    if system == "Darwin":
        out = subprocess.run(["launchctl", "print", f"gui/{_uid()}/{LABEL}"], capture_output=True, text=True)
        if out.returncode != 0:
            return None
        import re

        m = re.search(r"^\s*pid = (\d+)", out.stdout, re.MULTILINE)
        return int(m.group(1)) if m else None
    if system == "Linux":
        out = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", "mediavault.service"],
                             capture_output=True, text=True)
        value = out.stdout.strip()
        return int(value) if value.isdigit() and int(value) > 0 else None
    return None


def _loaded_macos() -> bool:
    return subprocess.run(["launchctl", "print", f"gui/{_uid()}/{LABEL}"], capture_output=True).returncode == 0


def start() -> str:
    """Start the installed service (no-op if it's already running)."""
    if not installed():
        raise RuntimeError("The service isn't installed yet: run `media-library install`.")
    system = platform.system()
    if system == "Darwin":
        if not _loaded_macos():
            r = subprocess.run(["launchctl", "bootstrap", f"gui/{_uid()}", str(_plist_path())], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip()}")
        else:
            subprocess.run(["launchctl", "kickstart", f"gui/{_uid()}/{LABEL}"], capture_output=True)
        return "Started."
    subprocess.run(["systemctl", "--user", "start", "mediavault.service"], check=True)
    return "Started."


def stop() -> str:
    """Stop it until the next start (or login). Unloads it from launchd, since
    a plain kill would just be undone by KeepAlive."""
    system = platform.system()
    if system == "Darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{_uid()}/{LABEL}"], capture_output=True)
        return "Stopped."
    subprocess.run(["systemctl", "--user", "stop", "mediavault.service"], check=True)
    return "Stopped."


def restart() -> str:
    system = platform.system()
    if system == "Darwin":
        if not _loaded_macos():
            return start()
        r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{_uid()}/{LABEL}"], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"launchctl kickstart failed: {r.stderr.strip()}")
        return "Restarted."
    subprocess.run(["systemctl", "--user", "restart", "mediavault.service"], check=True)
    return "Restarted."


# ------------------------------------------------------------------- macOS

def _install_macos(bin_path: Path, project_dir: Path, config_path: Path, port: int, hosts: list[str]) -> str:
    logs = _log_dir()
    plist = _plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)

    host_args = "".join(f"        <string>--host</string>\n        <string>{h}</string>\n" for h in hosts)
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{bin_path}</string>
        <string>web</string>
        <string>--no-open-browser</string>
{host_args}        <string>--port</string>
        <string>{port}</string>
    </array>
    <key>WorkingDirectory</key><string>{project_dir}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>MEDIAVAULT_CONFIG</key><string>{config_path}</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{logs / "service.log"}</string>
    <key>StandardErrorPath</key><string>{logs / "service.err.log"}</string>
</dict>
</plist>
"""
    plist.write_text(content)

    user = getpass.getuser()
    subprocess.run(["launchctl", "bootout", f"gui/{_uid()}", str(plist)], capture_output=True)
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{_uid()}", str(plist)], capture_output=True, text=True
    )
    if result.returncode != 0:
        # Older macOS fallback.
        result = subprocess.run(["launchctl", "load", "-w", str(plist)], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"launchctl failed: {result.stderr.strip()}")

    return (
        f"Installed launchd agent at {plist}\n"
        f"Runs as {user}, starts at login, restarts if it crashes.\n"
        f"Logs: {logs / 'service.log'}"
    )


def _uninstall_macos() -> str:
    plist = _plist_path()
    subprocess.run(["launchctl", "bootout", f"gui/{_uid()}", str(plist)], capture_output=True)
    subprocess.run(["launchctl", "unload", "-w", str(plist)], capture_output=True)
    if plist.exists():
        plist.unlink()
    return f"Removed launchd agent ({plist})."


def _uid() -> int:
    import os

    return os.getuid()


# ------------------------------------------------------------------ Linux

def _install_linux(bin_path: Path, project_dir: Path, config_path: Path, port: int, hosts: list[str]) -> str:
    logs = _log_dir()
    unit = _unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)

    host_args = "".join(f"--host {h} " for h in hosts)
    content = f"""[Unit]
Description=MediaVault background scanner/web UI

[Service]
Type=simple
WorkingDirectory={project_dir}
Environment=MEDIAVAULT_CONFIG={config_path}
ExecStart={bin_path} web --no-open-browser {host_args}--port {port}
Restart=on-failure
StandardOutput=append:{logs / "service.log"}
StandardError=append:{logs / "service.err.log"}

[Install]
WantedBy=default.target
"""
    unit.write_text(content)

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "mediavault.service"], check=True)

    return f"Installed systemd user unit at {unit}\nStarts at login, restarts on failure.\nLogs: {logs / 'service.log'}"


def _uninstall_linux() -> str:
    subprocess.run(["systemctl", "--user", "disable", "--now", "mediavault.service"], capture_output=True)
    unit = _unit_path()
    if unit.exists():
        unit.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return f"Removed systemd user unit ({unit})."
