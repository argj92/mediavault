"""Getting a network-drive root back when it drops off.

A root on an SMB/AFP/NFS share is only "there" while macOS has it mounted
under /Volumes, and mounting can need a login. Three things go wrong in
practice, each handled here:

- the share isn't mounted (logged out, rebooted, NAS asleep): `reconnect`
  asks macOS to mount it again from the root's saved URL. With the password
  saved in Keychain that happens silently; without, macOS shows its login
  dialog on *this* machine's screen. MediaVault never sees or stores a password.
- it was mounted again but under a different name -- macOS appends -1, -2...
  when the old mount point is still claimed -- so the saved path no longer
  exists: `candidate_paths` finds where it actually is.
- the service tried while the share was down and won't look again for a
  while: `try_auto` retries on its own, throttled.
"""
from __future__ import annotations

import platform
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("smb", "afp", "nfs", "cifs")
VOLUMES = Path("/Volumes")
AUTO_RETRY_SECONDS = 10 * 60


def validate_mount_url(url: str) -> str:
    """The cleaned URL, or ValueError. The UI has no login, and `open` will
    launch whatever it's given (apps, files), so only a network-share URL --
    never a path, file:// or custom scheme -- is accepted."""
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.hostname or any(c in url for c in "\n\r\0 "):
        raise ValueError("Use a network share address like smb://nas.local/ShareName (no spaces -- write them as %20).")
    return url


def candidate_paths(path: str | Path, volumes: Path = VOLUMES) -> list[Path]:
    """Other places the same folder may now be mounted: for
    /Volumes/akashjain/External Data/Videos, look for /Volumes/akashjain-1,
    -2... holding the same subfolder."""
    p = Path(path)
    try:
        rel = p.relative_to(volumes)
    except ValueError:
        return []
    if not rel.parts:
        return []
    name, rest = rel.parts[0], Path(*rel.parts[1:])
    pattern = re.compile(rf"^{re.escape(name)}-\d+$")
    found = []
    try:
        entries = sorted(volumes.iterdir())
    except OSError:
        return []
    for entry in entries:
        if pattern.match(entry.name):
            cand = entry / rest
            try:
                if cand.exists():
                    found.append(cand)
            except OSError:
                continue
    return found


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def reconnect(url: str, path: str | Path, wait_seconds: float = 8.0) -> tuple[bool, str]:
    """Ask macOS to mount `url`; wait briefly for `path` to appear. Returns
    (reachable, message)."""
    url = validate_mount_url(url)
    if platform.system() != "Darwin":
        return False, "Automatic reconnect is only supported on macOS; mount the share and use Scan now."
    try:
        subprocess.run(["open", url], check=True, timeout=15, capture_output=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Couldn't ask macOS to connect: {exc}"
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if _exists(Path(path)):
            return True, "Connected."
        time.sleep(0.5)
    return False, (
        "Asked macOS to connect. If a login box appeared on this Mac's screen, enter the password there "
        "and tick “Remember this password in my keychain” so it can reconnect by itself next time."
    )


def try_auto(conn, root_row, now: float | None = None) -> bool:
    """For the background scan: if this root has a mount URL and its folder is
    missing, try to bring it back, at most once per AUTO_RETRY_SECONDS (a
    share that needs a password would otherwise throw a login dialog up every
    cycle). True if the folder is reachable afterwards."""
    from . import db

    if not root_row["mount_url"]:
        return False
    now = time.time() if now is None else now
    key = f"last_reconnect:{root_row['label']}"
    if now - (db.get_worker_state(conn, key, 0) or 0) < AUTO_RETRY_SECONDS:
        return False
    db.set_worker_state(conn, key, now)
    conn.commit()
    ok, _ = reconnect(root_row["mount_url"], root_row["path"], wait_seconds=10.0)
    return ok
