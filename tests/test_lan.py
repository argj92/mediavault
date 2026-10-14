"""zeroconf is optional: its absence must not break importing the app or
starting with the LAN side enabled."""
import importlib
import sys

import pytest

from mediavault import discovery, lan
from mediavault.config import AppConfig


def test_discovery_imports_without_zeroconf(monkeypatch):
    for name in [m for m in sys.modules if m == "zeroconf" or m.startswith("zeroconf.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "zeroconf", None)   # `import zeroconf` raises ImportError
    try:
        mod = importlib.reload(discovery)
        assert mod.Zeroconf is None
        with pytest.raises(mod.ZeroconfMissing, match="mediavault\\[lan\\]"):
            mod.require_zeroconf()
        assert mod.PeerInfo("p", "m", "1.2.3.4", 1).port == 1   # the rest still works
    finally:
        monkeypatch.undo()
        importlib.reload(discovery)


def test_lan_start_survives_missing_zeroconf(tmp_path, monkeypatch):
    config = AppConfig(
        machine="m", db_path=tmp_path / "m.db", hash_algo="sha256", tmdb_api_key_env="X",
        video_extensions=[".mkv"], scan_interval_minutes=15, peer_port=0, enable_lan_discovery=True,
    )
    from mediavault import db
    with db.connect(config.db_path) as conn:
        db.init_db(conn)
    monkeypatch.setattr(discovery, "Zeroconf", None)
    d = lan.LanDiscovery(config)
    try:
        d.start()
        assert d.browser is None and d.own_peer_id
    finally:
        d.stop()
