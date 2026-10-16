"""Lifecycle of the opt-in LAN side (config.enable_lan_discovery): the narrow
peer_api server on all interfaces, plus mDNS advertise/browse.

Kept out of web/app.py so it can be started off the event loop and shut down
cleanly. Two things the old inline version got wrong:

- zeroconf's synchronous calls raise EventLoopBlocked on a thread that's
  running an asyncio loop, and joining its setup thread inside the (async)
  lifespan stalled the whole UI's startup for up to 10 s. `start()` is plain
  blocking code meant for `asyncio.to_thread`.
- zeroconf is optional. Without it the peer API still serves (Tailscale
  discovery and manual pairing work); only mDNS is skipped, with a warning
  rather than a crash.
"""
from __future__ import annotations

import logging
import threading

from . import db, discovery

log = logging.getLogger("mediavault.lan")


class LanDiscovery:
    def __init__(self, config):
        self.config = config
        self.browser = None
        self._zc = None
        self._advertiser = None
        self._server = None
        self.own_peer_id: str | None = None

    def start(self) -> None:
        import uvicorn

        from . import peer_api

        with db.connect(self.config.db_path) as conn:
            self.own_peer_id = db.get_or_create_peer_id(conn)

        peer_api.app.state.config = self.config
        self._server = uvicorn.Server(
            uvicorn.Config(peer_api.app, host="0.0.0.0", port=self.config.peer_port, log_level="warning")
        )
        threading.Thread(target=self._server.run, daemon=True, name="mediavault-peer-api").start()

        try:
            discovery.require_zeroconf()
        except discovery.ZeroconfMissing as exc:
            log.warning("%s -- peers won't be found automatically (Tailscale discovery still works)", exc)
            return
        self._zc = discovery.Zeroconf()
        self._advertiser = discovery.Advertiser(self._zc, self.config.machine, self.own_peer_id, self.config.peer_port)
        self._advertiser.start()
        self.browser = discovery.Browser(self._zc, self.own_peer_id)
        self.browser.start()

    def stop(self) -> None:
        for part in (self._advertiser, self.browser):
            if part is not None:
                part.stop()
        if self._zc is not None:
            self._zc.close()
        if self._server is not None:
            self._server.should_exit = True
