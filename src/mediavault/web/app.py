"""The local web UI: a single page with collapsible sections. Also what the
desktop GUI wraps in a native window (see mediavault.gui.app) — one UI, two
ways to launch it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import platform
import shutil
import sqlite3
import subprocess
import time
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import browse, catalog, cloud, db, dedupe, icloud, metadata, peers, protection, reconnect, recycle_bin, schedule, scheduler, suggestions, sync
from ..config import AppConfig, RootConfig
from ..lan import LanDiscovery
from .guard import RequestGuard

BASE_DIR = Path(__file__).parent
log = logging.getLogger("mediavault.web")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config: AppConfig = app.state.config
    with db.connect(config.db_path) as conn:
        db.init_db(conn)
        seed_roots_if_empty(conn, config)
    worker = scheduler.BackgroundWorker(config)
    worker.start()  # kicks off the first scan in the background; doesn't block startup
    app.state.worker = worker

    lan = None
    if config.enable_lan_discovery:
        lan = LanDiscovery(config)
        try:
            await asyncio.to_thread(lan.start)  # blocking socket/mDNS work, off the event loop
        except Exception:  # noqa: BLE001 - the local UI must come up even if the LAN side can't
            log.exception("LAN discovery failed to start; continuing without it")
        app.state.own_peer_id = lan.own_peer_id
        app.state.browser = lan.browser

    yield

    worker.stop()
    if lan is not None:
        await asyncio.to_thread(lan.stop)


app = FastAPI(title="MediaVault", lifespan=lifespan)
app.add_middleware(RequestGuard)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def human_bytes(n) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def time_ago(unix_ts) -> str:
    if unix_ts is None:
        return "never"
    seconds = max(0, time.time() - unix_ts)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def static_url(name: str) -> str:
    """/static/<name> with its mtime as a version, so a browser that cached an
    old stylesheet/script (the server sends no Cache-Control, so browsers
    reuse them heuristically) fetches the new one after an update instead of
    rendering new markup with old CSS."""
    try:
        version = int((BASE_DIR / "static" / name).stat().st_mtime)
    except OSError:
        version = 0
    return f"/static/{name}?v={version}"


def time_until(unix_ts) -> str:
    if unix_ts is None:
        return "not scheduled"
    seconds = unix_ts - time.time()
    if seconds <= 30:
        return "any moment"
    if seconds < 3600:
        return f"in {round(seconds / 60)} min"
    if seconds < 86400:
        return f"in {seconds / 3600:.1f} h".replace(".0 h", " h")
    return f"in {round(seconds / 86400)} d"


templates.env.globals["static_url"] = static_url
templates.env.filters["human_bytes"] = human_bytes
templates.env.filters["time_ago"] = time_ago
templates.env.filters["when"] = lambda ts: "?" if ts is None else time.strftime("%-d %b %H:%M", time.localtime(ts))
templates.env.filters["time_until"] = time_until
templates.env.filters["duration"] = lambda sec: "—" if sec is None else schedule.human_duration(sec)

# Sidebar: (group, [(section id, label)]). Section ids match the <details id>
# blocks in index.html.
templates.env.globals["nav_groups"] = [
    ("Library", [("library", "Browse"), ("roots", "Roots")]),
    ("Keep in order", [("sync", "Sync"), ("promote", "Promote"), ("duplicates", "Duplicates"), ("suggestions", "Suggestions")]),
    ("Backups & devices", [("protection", "Protection"), ("peers", "Peers")]),
    ("Storage", [("icloud", "iCloud"), ("recycle-bin", "Recycle bin"), ("metadata", "Metadata")]),
]
# Counts that mean "something wants your attention" (amber badge, and a chip in
# the status strip when non-zero): key -> (singular, plural, section anchor).
ATTENTION = {
    "duplicates": ("duplicate group", "duplicate groups", "duplicates"),
    "sync": ("root out of sync", "roots out of sync", "sync"),
    "promote": ("file to promote", "files to promote", "promote"),
    "suggestions": ("new file found", "new files found", "suggestions"),
    "peers": ("pairing request", "pairing requests", "peers"),
}
templates.env.globals["attn_sections"] = set(ATTENTION)
templates.env.globals["attn_counts"] = {}


def attention_counts(dup_groups, sync_statuses, promote_count, untracked_count, quarantine_count, pending_pairings) -> dict:
    """What the sidebar badges and status-strip chips show. A pure function of
    values the full page has already computed (index) or that the cheap poll
    endpoint computes itself, so both agree."""
    return {
        "duplicates": len(dup_groups),
        "sync": sum(1 for s in sync_statuses if s["plan"].out_of_sync),
        "promote": promote_count,
        "suggestions": untracked_count,
        "peers": len(pending_pairings),
        "recycle-bin": quarantine_count,
    }


def root_reachable(path) -> bool:
    try:
        return Path(path).exists()
    except OSError:  # a stale network mount can raise instead of answering
        return False


def describe_root(root, status: dict, reachable: bool) -> dict:
    """One root's state for the UI, as words: shared by the full page and the
    poll endpoint so the two can never disagree about what a root is doing.
    `state` drives colour/icon; `text` is the card line; `short` the chip."""
    active, last, abandoned = status["active"], status["last_finished"], status["abandoned"]
    if not root["enabled"]:
        return {"state": "disabled", "text": "Disabled — not scanned", "short": "disabled", "detail": ""}
    if active:
        n = active["files_scanned"] or 0
        return {"state": "scanning", "text": f"Scanning… {n:,} file(s) so far", "short": f"scanning {n:,}", "detail": ""}
    if not reachable:
        since = f" Last scanned {time_ago(last['finished_at'])}." if last else ""
        return {"state": "offline", "text": f"Offline — the folder isn't reachable (drive or share not mounted?).{since}",
                "short": "offline", "detail": root["path"]}
    if abandoned:
        n = abandoned["files_scanned"] or 0
        why = abandoned["error"] or "never finished — the process may have crashed, been force-quit, or slept mid-scan"
        return {"state": "failed", "text": f"{n:,} file(s) indexed, but the last scan didn't finish — Scan now to retry",
                "short": "scan incomplete", "detail": why}
    if last:
        n = last["files_scanned"] or 0
        try:
            away = last["placeholders"] or 0
        except (KeyError, IndexError):
            away = 0
        extra = f", {away:,} in the cloud only (not downloaded, so not hashed)" if away else ""
        return {"state": "ok", "text": f"Scanned {time_ago(last['finished_at'])} — {n:,} file(s){extra}",
                "short": f"scanned {time_ago(last['finished_at'])}", "detail": ""}
    return {"state": "never", "text": "Not scanned yet", "short": "not scanned", "detail": ""}


def scan_status_for(conn, roots) -> dict:
    status = {}
    for r in roots:
        active = db.get_active_scan(conn, r["label"])
        last_finished = None if active else db.get_last_finished_scan(conn, r["label"])
        status[r["label"]] = {
            "active": active,
            "last_finished": last_finished,
            # A scan that crashed/was killed mid-way still committed real
            # progress (scan_root commits periodically) -- surface that
            # instead of falsely claiming "never scanned yet" once both
            # of the above come back empty.
            "abandoned": None if (active or last_finished) else db.get_abandoned_scan(conn, r["label"]),
        }
    return status


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


def get_conn(request: Request):
    return db.connect(get_config(request).db_path)


def flash_redirect(anchor: str, message: str) -> RedirectResponse:
    """Redirects back to the single page, scrolled to `anchor` (e.g. '#roots'),
    with a flash message."""
    return RedirectResponse(url=f"/?msg={urllib.parse.quote(message)}{anchor}", status_code=303)


_SECTION_ANCHORS = {
    "duplicates": "#duplicates", "sync": "#sync", "promote": "#promote", "roots": "#roots",
    "recycle-bin": "#recycle-bin", "metadata": "#metadata", "catalog": "#protection", "peers": "#peers",
}


@app.exception_handler(OSError)
async def drive_error_handler(request: Request, exc: OSError):
    """Safety net for every route that touches a drive. The drives here are
    external/network volumes that routinely go away (unmounted share, sleeping
    NAS, unplugged USB), and an OSError (FileNotFoundError, PermissionError,
    ConnectionError, ...) escaping a handler used to surface as a bare
    "Internal Server Error" page with nothing changed and no hint why. Turn
    it into a message on the dashboard instead; the traceback still goes to
    the log. Routes with a specific, better message handle their own errors
    first (see duplicates_quarantine)."""
    log.error("drive error handling %s %s", request.method, request.url.path, exc_info=exc)
    message = (
        "A file operation failed -- a drive or network share may be unmounted or unreachable. "
        f"Nothing further was changed. ({type(exc).__name__}: {exc.strerror or exc})"
    )
    if request.method == "GET":
        return templates.TemplateResponse(request, "error.html", {"message": message}, status_code=503)
    section = _SECTION_ANCHORS.get(request.url.path.strip("/").split("/")[0], "")
    return flash_redirect(section, message)


def configure(config: AppConfig) -> FastAPI:
    """Attaches this machine's config before the app starts serving. Called
    by the CLI before handing `app` to uvicorn (mediavault web/gui)."""
    app.state.config = config
    return app


def seed_roots_if_empty(conn: sqlite3.Connection, config: AppConfig) -> None:
    if db.list_roots(conn) or not config.seed_roots:
        return
    for r in config.seed_roots:
        db.upsert_root(conn, r.label, str(r.path), r.role, r.mode)


# ----------------------------------------------------------------------- api

@app.get("/api/status")
def api_status(request: Request):
    """Small JSON summary for external dashboards (e.g. Donna's hub) to poll.
    Deliberately cheap — skips the library tree walk and iCloud/protection
    scans the full index() page does — so it's safe to hit on every refresh.

    This is a contract with out-of-repo consumers, so keep field *types*
    stable: per root, `last_scan` is the unix timestamp (float) of the last
    finished scan or null -- consumers do arithmetic on it directly. New
    detail goes in new sibling keys (`last_scan_files`), never by changing
    what an existing key holds."""
    config = get_config(request)
    with get_conn(request) as conn:
        roots = db.list_roots(conn)
        scan_status = {}
        for r in roots:
            active = db.get_active_scan(conn, r["label"])
            last_finished = None if active else db.get_last_finished_scan(conn, r["label"])
            scan_status[r["label"]] = {
                "active": bool(active),
                "last_scan": None if last_finished is None else last_finished["finished_at"],
                "last_scan_files": None if last_finished is None else last_finished["files_scanned"],
            }
        total_files = sum(1 for _ in db.all_files(conn))
        total_bytes = sum((f["size"] or 0) for f in db.all_files(conn) if not f["is_placeholder"])
        dup_groups = dedupe.find_duplicates(conn)
        untracked = suggestions.untracked_media(conn, config.video_extensions)
        quarantine_items = db.list_quarantine(conn)

    return JSONResponse({
        "machine": config.machine,
        "roots": [
            {
                "label": r["label"],
                "role": r["role"],
                "enabled": bool(r["enabled"]),
                "scanning": scan_status[r["label"]]["active"],
                "last_scan": scan_status[r["label"]]["last_scan"],
                "last_scan_files": scan_status[r["label"]]["last_scan_files"],
            }
            for r in roots
        ],
        "total_files": total_files,
        "total_bytes": total_bytes,
        "duplicate_groups": len(dup_groups),
        "wasted_bytes": dedupe.total_wasted_bytes(dup_groups),
        "untracked_count": len([u for u in untracked if not u["already_in_library"]]),
        "quarantine_count": len(quarantine_items),
        "any_scan_active": any(s["active"] for s in scan_status.values()),
    })


@app.get("/api/ui-state")
def api_ui_state(request: Request, counts: int = 1):
    """What the page polls to stay live without reloading: each root's state,
    and (unless `counts=0`) the attention counts. Roots are a few indexed
    lookups plus an exists() each; the counts re-derive duplicates and sync
    plans, so the page asks for them less often while a scan is running."""
    config = get_config(request)
    with get_conn(request) as conn:
        roots = db.list_roots(conn)
        status = scan_status_for(conn, roots)
        payload = {
            "server_time": time.time(),
            "roots": [
                {
                    "label": r["label"],
                    "scanned_at": status[r["label"]]["last_finished"]["finished_at"] if status[r["label"]]["last_finished"] else None,
                    **describe_root(r, status[r["label"]], root_reachable(r["path"])),
                }
                for r in roots
            ],
            "any_scan_active": any(s["active"] for s in status.values()),
            "schedule": schedule.snapshot(conn, config),
        }
        if counts:
            sync_statuses = sync.sync_status_for_all(conn)
            promote_count = sum(len(s["plan"].copy_to_a) for s in sync_statuses)
            untracked = suggestions.untracked_media(conn, config.video_extensions)
            payload["counts"] = attention_counts(
                dedupe.find_duplicates(conn), sync_statuses, promote_count,
                len([u for u in untracked if not u["already_in_library"]]),
                len(db.list_quarantine(conn)), db.list_pending_pairings(conn),
            )
    return JSONResponse(payload)


# --------------------------------------------------------------- single page

@app.get("/")
def index(request: Request, msg: str | None = None):
    config = get_config(request)
    with get_conn(request) as conn:
        roots = db.list_roots(conn)
        scan_status = scan_status_for(conn, roots)
        sched = schedule.snapshot(conn, config)
        any_scan_active = any(s["active"] for s in scan_status.values())
        total_files = sum(1 for _ in db.all_files(conn))
        total_bytes = sum((f["size"] or 0) for f in db.all_files(conn) if not f["is_placeholder"])
        dup_groups = dedupe.find_duplicates(conn)
        sync_statuses = sync.sync_status_for_all(conn)
        promote_candidates = sync.promotion_candidates(conn)
        ignored_promotions = db.list_ignored_promotions(conn)
        untracked = suggestions.untracked_media(conn, config.video_extensions)
        quarantine_items = db.list_quarantine(conn)
        metadata_files = metadata.files_needing_guess(conn, config.video_extensions)

        icloud_summaries = []
        for r in roots:
            if r["role"] != "icloud":
                continue
            p = Path(r["path"])
            if not p.exists():
                # The index still knows what's in there, so the space breakdown
                # stays useful (it's the last scan's view); only the live
                # download check needs the folder.
                icloud_summaries.append({"root": r, "error": "This folder isn't reachable right now — showing what the last scan found.",
                                         "space": icloud.analyze(conn, r["label"])})
                continue
            icloud_summaries.append(
                {"root": r, "summary": cloud.storage_summary(p), "largest": cloud.largest_downloaded_files(p, top_n=25),
                 "space": icloud.analyze(conn, r["label"])}
            )

        protection_report = protection.compute_protection(conn, config.machine)
        remote_catalogs = db.list_remote_catalogs(conn)

        known_peers = db.list_known_peers(conn)
        pending_pairings = db.list_pending_pairings(conn)

        library_roots = []
        for r in roots:
            if not r["enabled"] or r["role"] == "inbox":
                continue
            files = db.all_files(conn, root_label=r["label"])
            tree = browse.build_tree(files, config.video_extensions)
            library_roots.append({"root": r, "tree": tree, "video_count": tree.total_videos()})

    has_primary = any(r["role"] == "primary" and r["enabled"] for r in roots)
    root_states = {r["label"]: describe_root(r, scan_status[r["label"]], root_reachable(r["path"])) for r in roots}
    trash_scan = recycle_bin.scan_trash(top_n=50)
    new_untracked = [u for u in untracked if not u["already_in_library"]]

    paired_peer_ids = {p["peer_id"] for p in known_peers if p["paired"]}
    browser = getattr(request.app.state, "browser", None)
    mdns_peers = browser.list_peers() if browser is not None else []
    tailscale_candidates = getattr(request.app.state, "tailscale_candidates", [])
    seen_ids = set()
    discovered_peers = []
    for p in [*mdns_peers, *tailscale_candidates]:
        if p.peer_id in seen_ids or p.peer_id in paired_peer_ids:
            continue
        seen_ids.add(p.peer_id)
        discovered_peers.append(p)

    attn_counts = attention_counts(
        dup_groups, sync_statuses, len(promote_candidates), len(new_untracked), len(quarantine_items), pending_pairings
    )
    attn_counts["library"] = sum(lr["video_count"] for lr in library_roots)
    attn_counts["roots"] = len(roots)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "msg": msg,
            "root_states": root_states,
            "root_candidates": {
                r["label"]: reconnect.candidate_paths(r["path"])
                for r in roots if root_states[r["label"]]["state"] == "offline"
            },
            "offline_roots": {label for label, st in root_states.items() if st["state"] == "offline"},
            "attn_counts": attn_counts,
            "attention": ATTENTION,
            "machine": config.machine,
            "scan_interval": config.scan_interval_minutes,
            "sched": sched,
            "roots": roots,
            "scan_status": scan_status,
            "any_scan_active": any_scan_active,
            "has_primary": has_primary,
            "total_files": total_files,
            "total_bytes": total_bytes,
            "dup_groups": dup_groups,
            "wasted_bytes": dedupe.total_wasted_bytes(dup_groups),
            "dup_histogram": dedupe.size_histogram(dup_groups),
            "sync_statuses": sync_statuses,
            "untracked_items": untracked,
            "untracked_count": len(new_untracked),
            "quarantine_items": quarantine_items,
            "quarantine_count": len(quarantine_items),
            "quarantine_bytes": sum((q["size"] or 0) for q in quarantine_items),
            "metadata_files": metadata_files,
            "icloud_summaries": icloud_summaries,
            "trash_items": trash_scan["items"],
            "trash_bytes": trash_scan["total_bytes"],
            "trash_accurate": trash_scan["accurate"],
            "trash_access_errors": trash_scan["access_errors"],
            "access_app": str(recycle_bin.app_needing_access()),
            "protection_report": protection_report,
            "remote_catalogs": remote_catalogs,
            "library_roots": library_roots,
            "promote_candidates": promote_candidates,
            "ignored_promotions": ignored_promotions,
            "lan_discovery_enabled": config.enable_lan_discovery,
            "known_peers": known_peers,
            "pending_pairings": pending_pairings,
            "discovered_peers": discovered_peers,
        },
    )


# -------------------------------------------------------------------- roots

@app.post("/roots/pick-folder")
def roots_pick_folder():
    """Opens a native folder picker on the same machine the server runs on
    (this is a local, single-user app — the browser and the server share a
    desktop session) so you can browse to a folder instead of typing a path."""
    system = platform.system()
    # A person can leave a picker dialog open indefinitely (or the page can
    # be closed while it's still up) — always bound how long this blocks the
    # request, and let subprocess's own timeout handling kill the dialog.
    picker_timeout = 180

    if system == "Darwin":
        script = 'POSIX path of (choose folder with prompt "Select a folder for MediaVault to track")'
        try:
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=picker_timeout)
        except subprocess.TimeoutExpired:
            return JSONResponse({"path": None, "canceled": True, "error": "Timed out waiting for a folder to be picked."})
        if result.returncode != 0:
            reason = result.stderr.strip()
            canceled = "User canceled" in reason
            return JSONResponse({"path": None, "canceled": canceled, "error": None if canceled else reason})
        return JSONResponse({"path": result.stdout.strip()})

    if system == "Linux" and shutil.which("zenity"):
        try:
            result = subprocess.run(
                ["zenity", "--file-selection", "--directory"], capture_output=True, text=True, timeout=picker_timeout
            )
        except subprocess.TimeoutExpired:
            return JSONResponse({"path": None, "canceled": True, "error": "Timed out waiting for a folder to be picked."})
        if result.returncode != 0:
            return JSONResponse({"path": None, "canceled": True})
        return JSONResponse({"path": result.stdout.strip()})

    return JSONResponse(
        {"path": None, "error": "No folder picker available on this platform — type the path directly."},
        status_code=501,
    )


_PRIMARY_CONFLICT_MSG = (
    "There's already a primary root (the single basis every other root syncs "
    "against) — only one is supported. Use a different role, or change the "
    "existing primary's role first."
)


@app.post("/roots/add")
def roots_add(
    request: Request,
    label: str = Form(...),
    path: str = Form(...),
    role: str = Form(...),
    mode: str = Form(...),
):
    expanded = Path(path).expanduser()
    label = label.strip()
    config = get_config(request)
    with get_conn(request) as conn:
        if role == "primary" and db.count_primary_roots(conn, exclude_label=label) > 0:
            return flash_redirect("#roots", _PRIMARY_CONFLICT_MSG)
        db.upsert_root(conn, label, str(expanded), role, mode)

        # Scan it right away rather than making the user wait up to
        # scan_interval_minutes (or remember to click "Scan now") just to see
        # a root they only just added.
        from .. import scanner

        try:
            stats = scanner.scan_root(conn, RootConfig(label=label, path=expanded, role=role, mode=mode), hash_algo=config.hash_algo)
            msg = f"Added root '{label}': {stats['files_scanned']} files found."
        except OSError:
            # Not reachable at all (FileNotFoundError) or dropped mid-walk
            # (any other OSError -- see fix/scan-error-isolation) -- either
            # way this is a best-effort immediate scan; the background
            # worker will pick it up on its next cycle regardless.
            msg = f"Added root '{label}' — not reachable right now, will scan automatically once it is."
    return flash_redirect("#roots", msg)


@app.post("/roots/{label}/update")
def roots_update(request: Request, label: str, role: str = Form(...), mode: str = Form(...)):
    """Changes a root's role/mode in place — e.g. re-designating which root
    is primary, or turning a mirror backup into an icloud subset — without
    losing its indexed files the way remove+re-add would."""
    with get_conn(request) as conn:
        if db.get_root(conn, label) is None:
            return flash_redirect("#roots", f"No such root '{label}'.")
        if role == "primary" and db.count_primary_roots(conn, exclude_label=label) > 0:
            return flash_redirect("#roots", _PRIMARY_CONFLICT_MSG)
        db.update_root_role_mode(conn, label, role, mode)
    return flash_redirect("#roots", f"Updated '{label}' to {role}/{mode}.")


@app.post("/roots/{label}/mount-url")
def roots_mount_url(request: Request, label: str, mount_url: str = Form("")):
    """Where to re-mount a network-share root from (e.g. smb://nas.local/Videos).
    Empty clears it."""
    with get_conn(request) as conn:
        if db.get_root(conn, label) is None:
            return flash_redirect("#roots", f"No such root '{label}'.")
        if mount_url.strip():
            try:
                mount_url = reconnect.validate_mount_url(mount_url)
            except ValueError as exc:
                return flash_redirect("#roots", str(exc))
        db.set_root_mount_url(conn, label, mount_url.strip() or None)
    return flash_redirect("#roots", f"Saved the network address for '{label}'." if mount_url.strip() else f"Cleared it for '{label}'.")


@app.post("/roots/{label}/reconnect")
def roots_reconnect(request: Request, label: str):
    """Asks macOS to mount the root's saved share again. The address comes
    from what was saved on the root, never from this request."""
    with get_conn(request) as conn:
        row = db.get_root(conn, label)
    if row is None:
        return flash_redirect("#roots", f"No such root '{label}'.")
    if not row["mount_url"]:
        return flash_redirect("#roots", f"No network address saved for '{label}' yet -- add one under “Network login”.")
    ok, message = reconnect.reconnect(row["mount_url"], row["path"])
    if ok:
        message = f"'{label}' is connected again. Use Scan now to refresh it."
    return flash_redirect("#roots", message)


@app.post("/roots/{label}/use-path")
def roots_use_path(request: Request, label: str, path: str = Form(...)):
    """The share came back mounted under a different name (/Volumes/x-1).
    Only a place the server itself found for this root is accepted."""
    with get_conn(request) as conn:
        row = db.get_root(conn, label)
        if row is None:
            return flash_redirect("#roots", f"No such root '{label}'.")
        if path not in {str(c) for c in reconnect.candidate_paths(row["path"])}:
            return flash_redirect("#roots", "That isn't a place this folder was found -- nothing changed.")
        db.set_root_path(conn, label, path)
    return flash_redirect("#roots", f"'{label}' now points at {path}. Use Scan now to refresh it.")


@app.post("/roots/{label}/toggle")
def roots_toggle(request: Request, label: str):
    with get_conn(request) as conn:
        row = db.get_root(conn, label)
        if row:
            db.set_root_enabled(conn, label, not row["enabled"])
    return flash_redirect("#roots", f"Toggled '{label}'.")


def _worker(request: Request):
    return getattr(request.app.state, "worker", None)


@app.post("/schedule/run")
def schedule_run(request: Request):
    """"Scan all now": queue a full scan on the background worker and return
    at once -- the page shows progress as it runs."""
    worker = _worker(request)
    if worker is None:
        return flash_redirect("#roots", "Background scanning isn't running in this process.")
    if not worker.request_run(None):
        return flash_redirect("#roots", "A full scan is already running or queued — it will show up below when it finishes.")
    return flash_redirect("#roots", "Scan started. Progress shows on each root below.")


@app.post("/schedule/interval")
def schedule_interval(request: Request, minutes: int = Form(...)):
    config = get_config(request)
    with get_conn(request) as conn:
        try:
            minutes = schedule.set_interval_minutes(conn, minutes)
        except ValueError as exc:
            return flash_redirect("", str(exc))
        advice = schedule.advise(minutes, schedule.scan_estimates(conn))
    worker = _worker(request)
    if worker is not None:
        worker.reschedule()
    msg = f"Scans will run every {schedule.label_for(minutes)}."
    if advice["level"] != "ok" and advice["message"]:
        msg += " " + advice["message"]
    return flash_redirect("", msg)


@app.get("/api/schedule/advice")
def schedule_advice(request: Request, minutes: int):
    """What the UI shows under the frequency picker *before* it is saved."""
    with get_conn(request) as conn:
        return JSONResponse(schedule.advise(minutes, schedule.scan_estimates(conn)))


@app.post("/roots/{label}/scan")
def roots_scan(request: Request, label: str):
    config = get_config(request)
    worker = _worker(request)
    if worker is not None:
        # Background, like "Scan all now": the request used to sit there hashing
        # for as long as the drive took, and could double up with a scheduled scan.
        with get_conn(request) as conn:
            if db.get_root(conn, label) is None:
                return flash_redirect("#roots", f"No such root '{label}'.")
        if not worker.request_run([label]):
            return flash_redirect("#roots", f"'{label}' is already being scanned.")
        return flash_redirect("#roots", f"Scanning '{label}' in the background — other drives keep scanning alongside it.")
    with get_conn(request) as conn:
        row = db.get_root(conn, label)
        if row is None:
            return flash_redirect("#roots", f"No such root '{label}'.")
        root = RootConfig.from_row(row)
        from .. import scanner

        try:
            stats = scanner.scan_root(conn, root, hash_algo=config.hash_algo)
            msg = f"Scanned '{label}': {stats['files_scanned']} files, {stats['new_files']} new, {stats['updated_files']} updated."
        except OSError as exc:
            # Not mounted at all (FileNotFoundError) or dropped mid-walk
            # (any other OSError -- stale SMB/NFS handle, sleep/wake) --
            # either way, report it gracefully instead of a raw 500.
            msg = f"Could not scan '{label}': {exc}"
    return flash_redirect("#roots", msg)


@app.post("/roots/{label}/delete")
def roots_delete(request: Request, label: str, confirm: str = Form(...)):
    if confirm != "yes":
        return flash_redirect("#roots", "Deletion not confirmed.")
    with get_conn(request) as conn:
        db.remove_root(conn, label)
    return flash_redirect("#roots", f"Removed root '{label}' (and its index entries; files on disk untouched).")


# ----------------------------------------------------------------------- sync
# Single-basis model: one root is `primary`; every other mirror/subset root
# is compared against it automatically. No manual pairing.

@app.post("/sync/{label}")
def sync_root(request: Request, label: str):
    """The 'easy sync' button: fills this backup/subset root with whatever's
    missing that primary already has. Never deletes anything, and never
    pulls content the other way (backup-only files into primary) -- that's
    the Promote section's job, reviewed explicitly instead of bundled in
    here."""
    config = get_config(request)
    with get_conn(request) as conn:
        primary_row = db.get_primary_root(conn)
        if primary_row is None:
            return flash_redirect("#sync", "No primary root configured yet.")
        primary = RootConfig.from_row(primary_row)
        other_row = db.get_root(conn, label)
        if other_row is None:
            return flash_redirect("#sync", f"No such root '{label}'.")
        other = RootConfig.from_row(other_row)

        ignored = db.ignored_promotion_paths(conn)
        plan = sync.plan_sync(conn, primary, other, ignored_promotions=ignored)
        results = sync.execute_sync(primary, other, plan, apply_deletes=False, apply_promotions=False)

        from .. import scanner

        scanner.scan_root(conn, primary, hash_algo=config.hash_algo)
        scanner.scan_root(conn, other, hash_algo=config.hash_algo)
        new_plan = sync.plan_sync(conn, primary, other, ignored_promotions=ignored)

    msg = f"{label}: copied {results['copied_to_b']} to {label}."
    if results["errors"]:
        msg += f" {len(results['errors'])} error(s)."
    if new_plan.copy_to_a:
        msg += f" {len(new_plan.copy_to_a)} file(s) exist only on {label} — see Promote below."
    if new_plan.needs_confirmation:
        msg += " Some items need manual review (deletions or conflicts)."
    return flash_redirect("#sync", msg)


@app.post("/sync/{label}/restore")
def sync_restore_vanished(request: Request, label: str):
    """The safe answer to a deletion candidate: put the file back from the
    side that still has it, instead of deleting it from there too."""
    config = get_config(request)
    with get_conn(request) as conn:
        primary_row = db.get_primary_root(conn)
        other_row = db.get_root(conn, label)
        if primary_row is None or other_row is None:
            return flash_redirect("#sync", "Root no longer configured.")
        primary = RootConfig.from_row(primary_row)
        other = RootConfig.from_row(other_row)
        plan = sync.plan_sync(conn, primary, other)
        results = sync.execute_sync(primary, other, plan, restore_vanished=True)

        from .. import scanner

        scanner.scan_root(conn, primary, hash_algo=config.hash_algo)
        scanner.scan_root(conn, other, hash_algo=config.hash_algo)
    msg = f"{label}: restored {results['restored_to_b']} to {label}"
    if results["restored_to_a"]:
        msg += f" and {results['restored_to_a']} to primary"
    msg += "."
    if results["errors"]:
        msg += f" {len(results['errors'])} error(s)."
    return flash_redirect("#sync", msg)


@app.post("/sync/{label}/apply-deletes")
def sync_apply_deletes(request: Request, label: str, confirm: str = Form(...)):
    if confirm != "yes":
        return flash_redirect("#sync", "Deletion not confirmed — nothing changed.")
    config = get_config(request)
    with get_conn(request) as conn:
        primary_row = db.get_primary_root(conn)
        if primary_row is None:
            return flash_redirect("#sync", "No primary root configured yet.")
        primary = RootConfig.from_row(primary_row)
        other = RootConfig.from_row(db.get_root(conn, label))
        plan = sync.plan_sync(conn, primary, other)
        results = sync.execute_sync(primary, other, plan, apply_deletes=True)

        from .. import scanner

        scanner.scan_root(conn, primary, hash_algo=config.hash_algo)
        scanner.scan_root(conn, other, hash_algo=config.hash_algo)
    return flash_redirect(
        "#sync",
        f"{label}: deleted {results['deleted_from_a']} from primary, {results['deleted_from_b']} from {label}.",
    )


# -------------------------------------------------------------------- promote
# Files that showed up on a backup/mirror root but were never on primary.
# Reviewed here explicitly (move one, move all, or ignore for good) instead
# of being silently pulled into primary by "easy sync" above.

@app.post("/promote/move")
def promote_move(request: Request, source_label: str = Form(...), rel_path: str = Form(...)):
    with get_conn(request) as conn:
        primary_row = db.get_primary_root(conn)
        source_row = db.get_root(conn, source_label)
        if primary_row is None or source_row is None:
            return flash_redirect("#promote", "Root no longer configured.")
        sync.promote_one(RootConfig.from_row(source_row), RootConfig.from_row(primary_row), rel_path)
    return flash_redirect("#promote", f"Moved '{rel_path}' to primary. It'll show as in-sync on the next scan.")


@app.post("/promote/move-all")
def promote_move_all(request: Request):
    with get_conn(request) as conn:
        primary_row = db.get_primary_root(conn)
        if primary_row is None:
            return flash_redirect("#promote", "No primary root configured yet.")
        primary = RootConfig.from_row(primary_row)
        moved, errors = 0, 0
        for candidate in sync.promotion_candidates(conn):
            source_row = db.get_root(conn, candidate["source_label"])
            if source_row is None:
                continue
            try:
                sync.promote_one(RootConfig.from_row(source_row), primary, candidate["rel_path"])
                moved += 1
            except Exception:  # noqa: BLE001 — keep going, report the count
                errors += 1
    msg = f"Moved {moved} file(s) to primary."
    if errors:
        msg += f" {errors} failed."
    return flash_redirect("#promote", msg)


@app.post("/promote/ignore")
def promote_ignore(request: Request, rel_path: str = Form(...)):
    with get_conn(request) as conn:
        db.ignore_promotion(conn, rel_path)
    return flash_redirect("#promote", f"Won't suggest moving '{rel_path}' to primary again.")


@app.post("/promote/{ignore_id}/unignore")
def promote_unignore(request: Request, ignore_id: int):
    with get_conn(request) as conn:
        db.unignore_promotion(conn, ignore_id)
    return flash_redirect("#promote", "Removed from the ignored list — it may show up again next scan.")


# ----------------------------------------------------------------- duplicates

@app.post("/duplicates/quarantine")
def duplicates_quarantine(request: Request, root_label: str = Form(...), rel_path: str = Form(...)):
    config = get_config(request)
    quarantine_dir = config.db_path.parent / "quarantine"
    with get_conn(request) as conn:
        root_row = db.get_root(conn, root_label)
        if not root_row:
            return flash_redirect("#duplicates", "No such root.")
        try:
            dedupe.quarantine_duplicate(conn, quarantine_dir, Path(root_row["path"]), root_label, rel_path)
        except dedupe.QuarantineError as exc:
            # Caught *inside* the `with` on purpose: for "already gone" the
            # function drops the stale index row, and that has to commit so
            # the dashboard stops listing a file that no longer exists. Every
            # other QuarantineError changes nothing, so committing is a no-op.
            return flash_redirect("#duplicates", str(exc))
    return flash_redirect("#duplicates", f"Moved '{rel_path}' to quarantine.")


@app.post("/duplicates/quarantine-bulk")
def duplicates_quarantine_bulk(
    request: Request,
    hashes: list[str] = Form(...),
    mode: str = Form(...),
    confirm: str = Form(...),
):
    """Quarantine many duplicate groups at once -- the groups currently shown
    after filtering by size. mode=extras keeps one copy per designed root and
    quarantines the rest; mode=all quarantines every copy. See
    dedupe.quarantine_groups for why only groups the section is showing are
    eligible."""
    if confirm != "yes":
        return flash_redirect("#duplicates", "Not confirmed — nothing changed.")
    if mode not in ("extras", "all"):
        return flash_redirect("#duplicates", "Unknown bulk action — nothing changed.")
    config = get_config(request)
    quarantine_dir = config.db_path.parent / "quarantine"
    with get_conn(request) as conn:
        result = dedupe.quarantine_groups(conn, quarantine_dir, hashes, mode)

    what = "all copies" if mode == "all" else "extra copies"
    msg = f"Quarantined {result['quarantined']} {what} across {result['groups']} group(s) ({human_bytes(result['bytes'])})."
    if result["ignored"]:
        msg += f" {result['ignored']} group(s) skipped: no longer listed as duplicates."
    if result["skipped"]:
        msg += f" {len(result['skipped'])} file(s) couldn't be moved — first: {result['skipped'][0]}"
    return flash_redirect("#duplicates", msg)


# ---------------------------------------------------------------------- trash

@app.post("/icloud/reveal")
def icloud_reveal(request: Request, root_label: str = Form(...), rel_path: str = Form("")):
    """Shows a file or folder of an iCloud root in Finder, so it can be removed
    there (Finder → Move to Trash). Only paths the index knows under an iCloud
    root, and only inside that root -- nothing else can be opened from here."""
    import subprocess

    with get_conn(request) as conn:
        row = db.get_root(conn, root_label)
        if row is None or row["role"] != "icloud" or not icloud.is_known_path(conn, root_label, rel_path):
            return flash_redirect("#icloud", "That isn't something in an iCloud folder MediaVault tracks.")
        target = icloud.resolve_inside(row["path"], rel_path)
    if target is None or not target.exists():
        return flash_redirect("#icloud", "That item isn't there any more — rescan and try again.")
    if platform.system() != "Darwin":
        return flash_redirect("#icloud", "Showing in Finder only works on macOS.")
    try:
        subprocess.run(["open", "-R", str(target)], check=True, timeout=15, capture_output=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return flash_redirect("#icloud", f"Couldn't open Finder: {exc}")
    return flash_redirect("#icloud", f"Showing “{target.name}” in Finder on the Mac running MediaVault.")


@app.post("/system/full-disk-access")
def open_full_disk_access(request: Request):
    ok, msg = recycle_bin.open_full_disk_access_settings()
    return flash_redirect("#recycle-bin", msg if not ok else "Opened System Settings → Full Disk Access on the Mac running MediaVault.")


@app.post("/system/reveal-app")
def reveal_app(request: Request):
    ok, msg = recycle_bin.reveal_app_needing_access()
    return flash_redirect("#recycle-bin", msg if not ok else "Showing the app in Finder on the Mac running MediaVault — drag it into the Full Disk Access list.")


@app.post("/recycle-bin/empty")
def recycle_bin_empty(request: Request, paths: list[str] = Form(...), confirm: str = Form(...)):
    if confirm != "yes":
        return flash_redirect("#recycle-bin", "Not confirmed — nothing deleted.")
    result = recycle_bin.empty_items(paths, confirm=True)
    msg = f"Permanently deleted {len(result['deleted'])} item(s)."
    if result["errors"]:
        msg += f" {len(result['errors'])} error(s)."
    return flash_redirect("#recycle-bin", msg)


@app.post("/recycle-bin/quarantine/{qid}/restore")
def quarantine_restore(request: Request, qid: int):
    with get_conn(request) as conn:
        q = db.get_quarantine(conn, qid)
        if not q:
            return flash_redirect("#recycle-bin", "No such quarantined item.")
        root_row = db.get_root(conn, q["original_root"])
        if not root_row:
            return flash_redirect("#recycle-bin", "Original root no longer configured.")
        dest = Path(root_row["path"]) / q["original_rel_path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(q["quarantined_path"], str(dest))
        db.remove_quarantine_record(conn, qid)
    return flash_redirect("#recycle-bin", f"Restored '{q['original_rel_path']}'.")


@app.post("/recycle-bin/quarantine/purge-all")
def quarantine_purge_all(request: Request, confirm: str = Form(...), older_than_days: float | None = Form(None)):
    """Permanently deletes everything in MediaVault's quarantine, or only what
    has been there more than `older_than_days`."""
    if confirm != "yes":
        return flash_redirect("#recycle-bin", "Not confirmed — nothing deleted.")
    with get_conn(request) as conn:
        result = dedupe.purge_quarantine(conn, older_than_days)
    msg = f"Permanently deleted {result['purged']} quarantined file(s), freeing {human_bytes(result['bytes'])}."
    if result["errors"]:
        msg += f" {len(result['errors'])} could not be deleted: {result['errors'][0]}"
    return flash_redirect("#recycle-bin", msg)


@app.post("/recycle-bin/quarantine/{qid}/purge")
def quarantine_purge(request: Request, qid: int, confirm: str = Form(...)):
    if confirm != "yes":
        return flash_redirect("#recycle-bin", "Not confirmed — nothing deleted.")
    with get_conn(request) as conn:
        q = db.get_quarantine(conn, qid)
        if not q:
            return flash_redirect("#recycle-bin", "No such quarantined item.")
        Path(q["quarantined_path"]).unlink(missing_ok=True)
        db.remove_quarantine_record(conn, qid)
    return flash_redirect("#recycle-bin", "Permanently deleted.")


# ------------------------------------------------------------------- library

@app.get("/play/{file_id}")
def play_file(file_id: int, request: Request):
    """Streams a tracked video file for the browser's <video> element (Range
    requests are handled by FileResponse, so seeking/scrubbing works). Refuses
    anything missing, a placeholder, not a video extension, or hidden from
    browsing (dot-prefixed folder or under a .mediavault-hide marker) -- the
    same rule the Library section itself uses to decide what to show."""
    config = get_config(request)
    with get_conn(request) as conn:
        file_row = db.get_file_by_id(conn, file_id)
        if file_row is None or file_row["missing"] or file_row["is_placeholder"]:
            raise HTTPException(404, "File not tracked or not currently present.")

        root_row = db.get_root(conn, file_row["root_label"])
        if root_row is None or not root_row["enabled"]:
            raise HTTPException(404, "Root not tracked.")

        rel_path = file_row["rel_path"]
        if Path(rel_path).suffix.lower() not in {e.lower() for e in config.video_extensions}:
            raise HTTPException(404, "Not a video file.")

        marker_rows = conn.execute(
            "SELECT rel_path FROM files WHERE root_label=? AND missing=0", (root_row["label"],)
        ).fetchall()
        hidden_marker_folders = browse.marker_folders([r["rel_path"] for r in marker_rows])
        if browse.is_hidden(rel_path, hidden_marker_folders):
            raise HTTPException(404, "This file is in a hidden folder.")

    full_path = Path(root_row["path"]) / rel_path
    if not full_path.exists():
        raise HTTPException(404, "File isn't currently reachable on disk (root unmounted?).")

    media_type = mimetypes.guess_type(full_path.name)[0] or "application/octet-stream"
    return FileResponse(full_path, media_type=media_type, filename=full_path.name)


# ------------------------------------------------------------------ metadata

@app.post("/metadata/{file_id}/enrich")
def metadata_enrich(request: Request, file_id: int):
    config = get_config(request)
    with get_conn(request) as conn:
        row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if not row:
            return flash_redirect("#metadata", "No such file.")
        result = metadata.enrich_file(conn, row, config.tmdb_api_key)
    return flash_redirect("#metadata", f"Guessed '{result['title']}' ({result['year']}) — suggested: {result['suggested_name']}")


@app.post("/metadata/{file_id}/rename")
def metadata_rename(request: Request, file_id: int, new_name: str = Form(...)):
    with get_conn(request) as conn:
        row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if not row:
            return flash_redirect("#metadata", "No such file.")
        root_row = db.get_root(conn, row["root_label"])
        old_path = Path(root_row["path"]) / row["rel_path"]
        new_rel = str(Path(row["rel_path"]).parent / new_name)
        new_path = Path(root_row["path"]) / new_rel
        old_path.rename(new_path)
        conn.execute("UPDATE files SET rel_path=? WHERE id=?", (new_rel, file_id))
    return flash_redirect("#metadata", f"Renamed to '{new_name}'.")


# ------------------------------------------------------------------- catalog
# Cross-machine protection tracking: export this machine's index as a small
# portable JSON file (hash/size/path/role — never file contents), and import
# one exported from another machine, so protection.py can tell you which
# files only exist in one place without both machines being mounted at once.

@app.get("/catalog/export")
def catalog_export_download(request: Request):
    config = get_config(request)
    with get_conn(request) as conn:
        data = catalog.build_catalog(conn, config.machine)
    body = json.dumps(data, indent=2)
    filename = f"{config.machine}-catalog.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/catalog/import")
async def catalog_import_upload(request: Request, file: UploadFile):
    config = get_config(request)
    try:
        raw = await file.read()
        data = json.loads(raw)
        if not isinstance(data, dict) or "machine" not in data or "files" not in data:
            raise catalog.CatalogError("That file doesn't look like a mediavault catalog.")
        with get_conn(request) as conn:
            n = catalog.import_catalog(conn, data, config.machine)
    except (catalog.CatalogError, ValueError) as exc:
        return flash_redirect("#protection", str(exc))
    return flash_redirect("#protection", f"Imported {n} file(s) from '{data['machine']}'.")


@app.post("/catalog/{machine}/remove")
def catalog_remove_route(request: Request, machine: str):
    with get_conn(request) as conn:
        db.remove_remote_catalog(conn, machine)
    return flash_redirect("#protection", f"Removed catalog for '{machine}'.")


# ---------------------------------------------------------------------- peers
# Discovery (mDNS, always-on when enabled; Tailscale, probed on demand since
# it's a live network round-trip per device) feeds candidates into the same
# mutual-pairing handshake (peers.py) regardless of source. Nothing here is
# reachable from the LAN — that's peer_api.py's job, on a separate port.

@app.post("/peers/discover-tailscale")
def peers_discover_tailscale(request: Request):
    from .. import tailscale

    config = get_config(request)
    try:
        peers.require_lan_enabled(config)
    except peers.PairingError as exc:
        return flash_redirect("#peers", str(exc))
    if not tailscale.is_available():
        return flash_redirect("#peers", "Tailscale isn't installed on this machine.")
    found = tailscale.discover_tailnet_candidates(config.peer_port)
    request.app.state.tailscale_candidates = found
    return flash_redirect("#peers", f"Found {len(found)} mediavault instance(s) on your tailnet.")


@app.post("/peers/pair")
def peers_pair(request: Request, peer_id: str = Form(...), machine_name: str = Form(...), address: str = Form(...), port: int = Form(...)):
    from ..discovery import PeerInfo

    config = get_config(request)
    with get_conn(request) as conn:
        try:
            code = peers.initiate_pairing(conn, config, PeerInfo(peer_id=peer_id, machine_name=machine_name, address=address, port=port))
        except Exception as exc:  # noqa: BLE001 — network call to another machine, many ways to fail
            return flash_redirect("#peers", f"Could not reach {machine_name}: {exc}")
    return flash_redirect("#peers", f"Pairing code: {code} — confirm it matches what's shown on {machine_name}, then accept it there.")


@app.post("/peers/{peer_id}/accept")
def peers_accept(request: Request, peer_id: str):
    config = get_config(request)
    with get_conn(request) as conn:
        try:
            peers.accept_pending_pairing(conn, config, peer_id)
        except Exception as exc:  # noqa: BLE001
            return flash_redirect("#peers", f"Could not complete pairing: {exc}")
    return flash_redirect("#peers", "Paired.")


@app.post("/peers/{peer_id}/reject")
def peers_reject(request: Request, peer_id: str):
    with get_conn(request) as conn:
        peers.reject_pending_pairing(conn, peer_id)
    return flash_redirect("#peers", "Rejected.")


@app.post("/peers/{peer_id}/sync")
def peers_sync(request: Request, peer_id: str):
    config = get_config(request)
    with get_conn(request) as conn:
        peer_row = db.get_known_peer(conn, peer_id)
        if peer_row is None:
            return flash_redirect("#peers", "No such peer.")
        try:
            n = peers.sync_with_peer(conn, config, peer_row)
        except Exception as exc:  # noqa: BLE001
            return flash_redirect("#peers", f"Could not sync with {peer_row['machine_name']}: {exc}")
    return flash_redirect("#peers", f"Imported {n} file(s) from {peer_row['machine_name']}.")


@app.post("/peers/{peer_id}/remove")
def peers_remove(request: Request, peer_id: str):
    with get_conn(request) as conn:
        db.remove_known_peer(conn, peer_id)
    return flash_redirect("#peers", "Removed.")
