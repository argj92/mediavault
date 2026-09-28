"""The local web UI: a single page with collapsible sections. Also what the
desktop GUI wraps in a native window (see mediavault.gui.app) — one UI, two
ways to launch it.
"""
from __future__ import annotations

import json
import mimetypes
import platform
import shutil
import sqlite3
import subprocess
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import browse, catalog, cloud, db, dedupe, metadata, protection, recycle_bin, scheduler, suggestions, sync
from ..config import AppConfig, RootConfig

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    config: AppConfig = app.state.config
    with db.connect(config.db_path) as conn:
        db.init_db(conn)
        seed_roots_if_empty(conn, config)
    worker = scheduler.BackgroundWorker(config)
    worker.start()  # kicks off the first scan in the background; doesn't block startup
    app.state.worker = worker
    yield
    worker.stop()


app = FastAPI(title="MediaVault", lifespan=lifespan)
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


templates.env.filters["human_bytes"] = human_bytes


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


def get_conn(request: Request):
    return db.connect(get_config(request).db_path)


def flash_redirect(anchor: str, message: str) -> RedirectResponse:
    """Redirects back to the single page, scrolled to `anchor` (e.g. '#roots'),
    with a flash message."""
    return RedirectResponse(url=f"/?msg={urllib.parse.quote(message)}{anchor}", status_code=303)


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


# --------------------------------------------------------------- single page

@app.get("/")
def index(request: Request, msg: str | None = None):
    config = get_config(request)
    with get_conn(request) as conn:
        roots = db.list_roots(conn)
        total_files = sum(1 for _ in db.all_files(conn))
        total_bytes = sum((f["size"] or 0) for f in db.all_files(conn) if not f["is_placeholder"])
        dup_groups = dedupe.find_duplicates(conn)
        sync_statuses = sync.sync_status_for_all(conn)
        promote_candidates = sync.promotion_candidates(conn)
        ignored_promotions = db.list_ignored_promotions(conn)
        untracked = suggestions.untracked_media(conn, config.video_extensions)
        quarantine_items = db.list_quarantine(conn)
        metadata_files = conn.execute(
            "SELECT * FROM files WHERE missing=0 AND is_placeholder=0 AND title_guess IS NULL "
            "AND root_label IN (SELECT label FROM roots WHERE role != 'inbox') LIMIT 100"
        ).fetchall()

        icloud_summaries = []
        for r in roots:
            if r["role"] != "icloud":
                continue
            p = Path(r["path"])
            if not p.exists():
                icloud_summaries.append({"root": r, "error": "path not currently accessible"})
                continue
            icloud_summaries.append(
                {"root": r, "summary": cloud.storage_summary(p), "largest": cloud.largest_downloaded_files(p, top_n=25)}
            )

        protection_report = protection.compute_protection(conn, config.machine)
        remote_catalogs = db.list_remote_catalogs(conn)

        library_roots = []
        for r in roots:
            if not r["enabled"] or r["role"] == "inbox":
                continue
            files = db.all_files(conn, root_label=r["label"])
            tree = browse.build_tree(files, config.video_extensions)
            library_roots.append({"root": r, "tree": tree, "video_count": tree.total_videos()})

    has_primary = any(r["role"] == "primary" and r["enabled"] for r in roots)
    trash_scan = recycle_bin.scan_trash(top_n=50)
    new_untracked = [u for u in untracked if not u["already_in_library"]]

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "msg": msg,
            "machine": config.machine,
            "scan_interval": config.scan_interval_minutes,
            "roots": roots,
            "has_primary": has_primary,
            "total_files": total_files,
            "total_bytes": total_bytes,
            "dup_groups": dup_groups,
            "wasted_bytes": dedupe.total_wasted_bytes(dup_groups),
            "sync_statuses": sync_statuses,
            "untracked_items": untracked,
            "untracked_count": len(new_untracked),
            "quarantine_items": quarantine_items,
            "quarantine_count": len(quarantine_items),
            "metadata_files": metadata_files,
            "icloud_summaries": icloud_summaries,
            "trash_items": trash_scan["items"],
            "trash_bytes": trash_scan["total_bytes"],
            "trash_accurate": trash_scan["accurate"],
            "trash_access_errors": trash_scan["access_errors"],
            "protection_report": protection_report,
            "remote_catalogs": remote_catalogs,
            "library_roots": library_roots,
            "promote_candidates": promote_candidates,
            "ignored_promotions": ignored_promotions,
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


@app.post("/roots/{label}/toggle")
def roots_toggle(request: Request, label: str):
    with get_conn(request) as conn:
        row = db.get_root(conn, label)
        if row:
            db.set_root_enabled(conn, label, not row["enabled"])
    return flash_redirect("#roots", f"Toggled '{label}'.")


@app.post("/roots/{label}/scan")
def roots_scan(request: Request, label: str):
    config = get_config(request)
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
        dedupe.quarantine_duplicate(conn, quarantine_dir, Path(root_row["path"]), root_label, rel_path)
    return flash_redirect("#duplicates", f"Moved '{rel_path}' to quarantine.")


# ---------------------------------------------------------------------- trash

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
