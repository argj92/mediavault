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


def install(bin_path: Path, project_dir: Path, config_path: Path, port: int = 8420) -> str:
    system = platform.system()
    if system == "Darwin":
        return _install_macos(bin_path, project_dir, config_path, port)
    if system == "Linux":
        return _install_linux(bin_path, project_dir, config_path, port)
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


# ------------------------------------------------------------------- macOS

def _install_macos(bin_path: Path, project_dir: Path, config_path: Path, port: int) -> str:
    logs = _log_dir()
    plist = _plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)

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
        <string>--port</string>
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

def _install_linux(bin_path: Path, project_dir: Path, config_path: Path, port: int) -> str:
    logs = _log_dir()
    unit = _unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)

    content = f"""[Unit]
Description=MediaVault background scanner/web UI

[Service]
Type=simple
WorkingDirectory={project_dir}
Environment=MEDIAVAULT_CONFIG={config_path}
ExecStart={bin_path} web --no-open-browser --port {port}
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
