"""One mediavault per database.

Two instances against the same index (a launchd service plus a hand-started
`mediavault web`, the GUI plus the service...) each run their own scan worker,
re-hash the same files, fight over the notification baselines in
worker_state, and -- when one is an old version left running -- keep serving
stale behaviour from a port nobody remembers. SQLite tolerates the sharing;
nothing makes it a good idea.

`acquire` takes an exclusive advisory lock on `<db>.lock` for the life of the
process. The OS drops it when the process exits, however it dies, so there's
no stale-lock cleanup to get wrong.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


_held: dict[str, object] = {}  # keeps the file objects (and so the locks) alive


def acquire(db_path: Path) -> Path:
    """Take the lock or raise AlreadyRunning naming the process holding it."""
    lock_path = Path(str(db_path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.seek(0)
        holder = f.read().strip() or "unknown"
        f.close()
        raise AlreadyRunning(
            f"Another mediavault (pid {holder}) is already using {db_path}. "
            f"Use it, or stop it first (`kill {holder}`, or `mediavault service uninstall` if it's the background service)."
        ) from None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    _held[str(lock_path)] = f
    return lock_path
