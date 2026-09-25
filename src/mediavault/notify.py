"""Best-effort cross-platform desktop notifications. Never raises — a failed
notification should never take down a background scan."""
from __future__ import annotations

import platform
import shutil
import subprocess


def notify(title: str, message: str) -> bool:
    try:
        system = platform.system()
        if system == "Darwin":
            script = f'display notification {_osa_quote(message)} with title {_osa_quote(title)}'
            subprocess.run(["osascript", "-e", script], check=False, timeout=5)
            return True
        if system == "Linux" and shutil.which("notify-send"):
            subprocess.run(["notify-send", title, message], check=False, timeout=5)
            return True
        if system == "Windows":
            try:
                from win10toast import ToastNotifier  # type: ignore

                ToastNotifier().show_toast(title, message, duration=8, threaded=True)
                return True
            except ImportError:
                pass
        print(f"[notify] {title}: {message}")
        return False
    except Exception:  # noqa: BLE001 — notifications are best-effort
        return False


def _osa_quote(s: str) -> str:
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
