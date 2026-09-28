"""Desktop GUI: the exact same web UI (mediavault.web.app), just opened in a
native window instead of a browser tab, via pywebview. One UI to maintain,
two ways to launch it — `mediavault web` or `mediavault gui`.
"""
from __future__ import annotations

import socket
import threading
import time

import uvicorn

from ..config import AppConfig
from ..web.app import app, configure


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_gui(config: AppConfig) -> None:
    try:
        import webview  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "The desktop GUI needs the optional 'gui' extra: pip install 'mediavault[gui]'"
        ) from exc

    configure(config)
    port = _free_port()

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # server.started only flips true once uvicorn has bound the socket AND
    # our FastAPI lifespan startup (db init, seeding, background worker) has
    # finished. Pointing the window at the URL before that races the server:
    # the request fails, and pywebview never retries -- just shows a blank
    # window forever.
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise SystemExit("mediavault gui: the local server didn't start within 15s.")
        time.sleep(0.05)

    webview.create_window("MediaVault", f"http://127.0.0.1:{port}", width=1150, height=800)
    webview.start()
    server.should_exit = True
