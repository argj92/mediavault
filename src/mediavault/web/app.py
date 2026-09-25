"""The local web UI. Also what the desktop GUI wraps in a native window
(see mediavault.gui.app) — one UI, two ways to launch it.
"""
from __future__ import annotations

import sqlite3
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import cloud, db, dedupe, metadata, recycle_bin, scheduler, suggestions, sync
from ..config import AppConfig, RootConfig

BASE_DIR = Path(__file__).parent

app = FastAPI(title="MediaVault")
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


def flash_redirect(url: str, message: str) -> RedirectResponse:
    return RedirectResponse(url=f"{url}?msg={urllib.parse.quote(message)}", status_code=303)


def configure(config: AppConfig) -> FastAPI:
    """Attaches this machine's config before the app starts serving. Called
    by the CLI before handing `app` to uvicorn (mediavault web/gui)."""
    app.state.config = config
    return app


@app.on_event("startup")
def on_startup() -> None:
    config: AppConfig = app.state.config
    with db.connect(config.db_path) as conn:
        db.init_db(conn)
        seed_roots_if_empty(conn, config)
    worker = scheduler.BackgroundWorker(config)
    worker.start()  # blocks briefly for one synchronous reconcile, then backgrounds
    app.state.worker = worker


@app.on_event("shutdown")
def on_shutdown() -> None:
    worker = getattr(app.state, "worker", None)
    if worker:
        worker.stop()


def seed_roots_if_empty(conn: sqlite3.Connection, config: AppConfig) -> None:
    if db.list_roots(conn) or not config.seed_roots:
        return
    for r in config.seed_roots:
        db.upsert_root(conn, r.label, str(r.path), r.role, r.mode)


# ---------------------------------------------------------------- dashboard

@app.get("/")
def dashboard(request: Request, msg: str | None = None):
    config = get_config(request)
    with get_conn(request) as conn:
        roots = db.list_roots(conn)
        total_files = sum(1 for _ in db.all_files(conn))
        total_bytes = sum((f["size"] or 0) for f in db.all_files(conn) if not f["is_placeholder"])
        dup_groups = dedupe.find_duplicates(conn)
        pairs = db.list_sync_pairs(conn)
        untracked = [u for u in suggestions.untracked_media(conn, config.video_extensions) if not u["already_in_library"]]
        quarantine_count = len(db.list_quarantine(conn))
    trash_bytes = recycle_bin.total_trash_bytes()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "msg": msg,
            "machine": config.machine,
            "roots": roots,
            "total_files": total_files,
            "total_bytes": total_bytes,
            "dup_groups": dup_groups,
            "wasted_bytes": dedupe.total_wasted_bytes(dup_groups),
            "pairs": pairs,
            "untracked_count": len(untracked),
            "quarantine_count": quarantine_count,
            "trash_bytes": trash_bytes,
            "scan_interval": config.scan_interval_minutes,
        },
    )


# -------------------------------------------------------------------- roots

@app.get("/roots")
def roots_page(request: Request, msg: str | None = None):
    with get_conn(request) as conn:
        roots = db.list_roots(conn)
    return templates.TemplateResponse(request, "roots.html", {"request": request, "roots": roots, "msg": msg})


@app.post("/roots/add")
def roots_add(
    request: Request,
    label: str = Form(...),
    path: str = Form(...),
    role: str = Form(...),
    mode: str = Form(...),
):
    expanded = Path(path).expanduser()
    with get_conn(request) as conn:
        db.upsert_root(conn, label.strip(), str(expanded), role, mode)
    return flash_redirect("/roots", f"Added root '{label}'. It'll be picked up on the next scan cycle.")


@app.post("/roots/{label}/toggle")
def roots_toggle(request: Request, label: str):
    with get_conn(request) as conn:
        row = db.get_root(conn, label)
        if row:
            db.set_root_enabled(conn, label, not row["enabled"])
    return flash_redirect("/roots", f"Toggled '{label}'.")


@app.post("/roots/{label}/scan")
def roots_scan(request: Request, label: str):
    config = get_config(request)
    with get_conn(request) as conn:
        row = db.get_root(conn, label)
        if row is None:
            return flash_redirect("/roots", f"No such root '{label}'.")
        root = RootConfig.from_row(row)
        from .. import scanner

        try:
            stats = scanner.scan_root(conn, root, hash_algo=config.hash_algo)
            msg = f"Scanned '{label}': {stats['files_scanned']} files, {stats['new_files']} new, {stats['updated_files']} updated."
        except FileNotFoundError as exc:
            msg = f"Could not scan '{label}': {exc}"
    return flash_redirect("/roots", msg)


@app.post("/roots/{label}/delete")
def roots_delete(request: Request, label: str, confirm: str = Form(...)):
    if confirm != "yes":
        return flash_redirect("/roots", "Deletion not confirmed.")
    with get_conn(request) as conn:
        db.remove_root(conn, label)
    return flash_redirect("/roots", f"Removed root '{label}' (and its index entries; files on disk untouched).")


# ----------------------------------------------------------------- sync pairs

@app.get("/pairs")
def pairs_page(request: Request, msg: str | None = None):
    with get_conn(request) as conn:
        roots = db.list_roots(conn)
        pairs = []
        for p in db.list_sync_pairs(conn):
            root_a = db.get_root(conn, p["root_a"])
            root_b = db.get_root(conn, p["root_b"])
            if not root_a or not root_b:
                continue
            plan = sync.plan_sync(conn, RootConfig.from_row(root_a), RootConfig.from_row(root_b))
            pairs.append({"row": p, "plan": plan})
    return templates.TemplateResponse(request, "pairs.html", {"request": request, "roots": roots, "pairs": pairs, "msg": msg})


@app.post("/pairs/add")
def pairs_add(request: Request, root_a: str = Form(...), root_b: str = Form(...)):
    with get_conn(request) as conn:
        db.add_sync_pair(conn, root_a, root_b)
    return flash_redirect("/pairs", f"Watching {root_a} <-> {root_b}.")


@app.post("/pairs/{pair_id}/delete")
def pairs_delete(request: Request, pair_id: int):
    with get_conn(request) as conn:
        db.remove_sync_pair(conn, pair_id)
    return flash_redirect("/pairs", "Sync pair removed (folders themselves untouched).")


@app.post("/pairs/{pair_id}/sync")
def pairs_sync(request: Request, pair_id: int):
    """The 'easy sync' button: copies whatever's missing on either side.
    Never deletes anything — deletions/conflicts are reviewed separately."""
    with get_conn(request) as conn:
        pair = db.get_sync_pair(conn, pair_id)
        if not pair:
            return flash_redirect("/pairs", "No such sync pair.")
        root_a = RootConfig.from_row(db.get_root(conn, pair["root_a"]))
        root_b = RootConfig.from_row(db.get_root(conn, pair["root_b"]))
        plan = sync.plan_sync(conn, root_a, root_b)
        results = sync.execute_sync(root_a, root_b, plan, apply_deletes=False)

        from .. import scanner

        scanner.scan_root(conn, root_a, hash_algo=get_config(request).hash_algo)
        scanner.scan_root(conn, root_b, hash_algo=get_config(request).hash_algo)
        new_plan = sync.plan_sync(conn, root_a, root_b)
        status = "out_of_sync" if new_plan.out_of_sync else "in_sync"
        db.update_pair_status(conn, pair_id, status, synced=True)

    msg = f"Copied {results['copied_to_a']} file(s) to {pair['root_a']}, {results['copied_to_b']} to {pair['root_b']}."
    if results["errors"]:
        msg += f" {len(results['errors'])} error(s)."
    if new_plan.needs_confirmation:
        msg += " Some items need manual review (deletions or conflicts)."
    return flash_redirect("/pairs", msg)


@app.get("/pairs/{pair_id}")
def pair_detail(request: Request, pair_id: int, msg: str | None = None):
    with get_conn(request) as conn:
        pair = db.get_sync_pair(conn, pair_id)
        if not pair:
            return flash_redirect("/pairs", "No such sync pair.")
        root_a = RootConfig.from_row(db.get_root(conn, pair["root_a"]))
        root_b = RootConfig.from_row(db.get_root(conn, pair["root_b"]))
        plan = sync.plan_sync(conn, root_a, root_b)
    return templates.TemplateResponse(
        request, "pair_detail.html", {"pair": pair, "plan": plan, "msg": msg}
    )


@app.post("/pairs/{pair_id}/apply-deletes")
def pair_apply_deletes(request: Request, pair_id: int, confirm: str = Form(...)):
    if confirm != "yes":
        return flash_redirect(f"/pairs/{pair_id}", "Deletion not confirmed — nothing changed.")
    with get_conn(request) as conn:
        pair = db.get_sync_pair(conn, pair_id)
        root_a = RootConfig.from_row(db.get_root(conn, pair["root_a"]))
        root_b = RootConfig.from_row(db.get_root(conn, pair["root_b"]))
        plan = sync.plan_sync(conn, root_a, root_b)
        results = sync.execute_sync(root_a, root_b, plan, apply_deletes=True)

        from .. import scanner

        scanner.scan_root(conn, root_a, hash_algo=get_config(request).hash_algo)
        scanner.scan_root(conn, root_b, hash_algo=get_config(request).hash_algo)
    return flash_redirect(
        f"/pairs/{pair_id}",
        f"Deleted {results['deleted_from_a']} from A-side, {results['deleted_from_b']} from B-side.",
    )


# ----------------------------------------------------------------- duplicates

@app.get("/duplicates")
def duplicates_page(request: Request, msg: str | None = None):
    with get_conn(request) as conn:
        groups = dedupe.find_duplicates(conn)
    return templates.TemplateResponse(
        request,
        "duplicates.html",
        {"groups": groups, "wasted_bytes": dedupe.total_wasted_bytes(groups), "msg": msg},
    )


@app.post("/duplicates/quarantine")
def duplicates_quarantine(request: Request, root_label: str = Form(...), rel_path: str = Form(...)):
    config = get_config(request)
    quarantine_dir = config.db_path.parent / "quarantine"
    with get_conn(request) as conn:
        root_row = db.get_root(conn, root_label)
        if not root_row:
            return flash_redirect("/duplicates", "No such root.")
        dedupe.quarantine_duplicate(conn, quarantine_dir, Path(root_row["path"]), root_label, rel_path)
    return flash_redirect("/duplicates", f"Moved '{rel_path}' to quarantine.")


# ---------------------------------------------------------------- suggestions

@app.get("/suggestions")
def suggestions_page(request: Request):
    config = get_config(request)
    with get_conn(request) as conn:
        items = suggestions.untracked_media(conn, config.video_extensions)
    return templates.TemplateResponse(request, "suggestions.html", {"request": request, "items": items})


# --------------------------------------------------------------------- icloud

@app.get("/icloud")
def icloud_page(request: Request):
    with get_conn(request) as conn:
        icloud_roots = [r for r in db.list_roots(conn) if r["role"] == "icloud"]
    summaries = []
    for r in icloud_roots:
        p = Path(r["path"])
        if not p.exists():
            summaries.append({"root": r, "error": "path not currently accessible"})
            continue
        summary = cloud.storage_summary(p)
        largest = cloud.largest_downloaded_files(p, top_n=25)
        summaries.append({"root": r, "summary": summary, "largest": largest})
    return templates.TemplateResponse(request, "icloud.html", {"request": request, "summaries": summaries})


# ---------------------------------------------------------------------- trash

@app.get("/recycle-bin")
def recycle_bin_page(request: Request, msg: str | None = None):
    with get_conn(request) as conn:
        quarantine_items = db.list_quarantine(conn)
    trash_items = recycle_bin.list_trash_items(top_n=50)
    return templates.TemplateResponse(
        request,
        "recycle_bin.html",
        {"quarantine_items": quarantine_items, "trash_items": trash_items, "msg": msg},
    )


@app.post("/recycle-bin/empty")
def recycle_bin_empty(request: Request, paths: list[str] = Form(...), confirm: str = Form(...)):
    if confirm != "yes":
        return flash_redirect("/recycle-bin", "Not confirmed — nothing deleted.")
    result = recycle_bin.empty_items(paths, confirm=True)
    msg = f"Permanently deleted {len(result['deleted'])} item(s)."
    if result["errors"]:
        msg += f" {len(result['errors'])} error(s)."
    return flash_redirect("/recycle-bin", msg)


@app.post("/recycle-bin/quarantine/{qid}/restore")
def quarantine_restore(request: Request, qid: int):
    import shutil

    with get_conn(request) as conn:
        q = db.get_quarantine(conn, qid)
        if not q:
            return flash_redirect("/recycle-bin", "No such quarantined item.")
        root_row = db.get_root(conn, q["original_root"])
        if not root_row:
            return flash_redirect("/recycle-bin", "Original root no longer configured.")
        dest = Path(root_row["path"]) / q["original_rel_path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(q["quarantined_path"], str(dest))
        db.remove_quarantine_record(conn, qid)
    return flash_redirect("/recycle-bin", f"Restored '{q['original_rel_path']}'.")


@app.post("/recycle-bin/quarantine/{qid}/purge")
def quarantine_purge(request: Request, qid: int, confirm: str = Form(...)):
    if confirm != "yes":
        return flash_redirect("/recycle-bin", "Not confirmed — nothing deleted.")
    with get_conn(request) as conn:
        q = db.get_quarantine(conn, qid)
        if not q:
            return flash_redirect("/recycle-bin", "No such quarantined item.")
        Path(q["quarantined_path"]).unlink(missing_ok=True)
        db.remove_quarantine_record(conn, qid)
    return flash_redirect("/recycle-bin", "Permanently deleted.")


# ------------------------------------------------------------------ metadata

@app.get("/metadata")
def metadata_page(request: Request, msg: str | None = None):
    with get_conn(request) as conn:
        rows = conn.execute(
            "SELECT * FROM files WHERE missing=0 AND is_placeholder=0 AND title_guess IS NULL "
            "AND root_label IN (SELECT label FROM roots WHERE role != 'inbox') LIMIT 100"
        ).fetchall()
    return templates.TemplateResponse(request, "metadata.html", {"request": request, "files": rows, "msg": msg})


@app.post("/metadata/{file_id}/enrich")
def metadata_enrich(request: Request, file_id: int):
    config = get_config(request)
    with get_conn(request) as conn:
        row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if not row:
            return flash_redirect("/metadata", "No such file.")
        result = metadata.enrich_file(conn, row, config.tmdb_api_key)
    return flash_redirect("/metadata", f"Guessed '{result['title']}' ({result['year']}) — suggested: {result['suggested_name']}")


@app.post("/metadata/{file_id}/rename")
def metadata_rename(request: Request, file_id: int, new_name: str = Form(...)):
    with get_conn(request) as conn:
        row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if not row:
            return flash_redirect("/metadata", "No such file.")
        root_row = db.get_root(conn, row["root_label"])
        old_path = Path(root_row["path"]) / row["rel_path"]
        new_rel = str(Path(row["rel_path"]).parent / new_name)
        new_path = Path(root_row["path"]) / new_rel
        old_path.rename(new_path)
        conn.execute("UPDATE files SET rel_path=? WHERE id=?", (new_rel, file_id))
    return flash_redirect("/metadata", f"Renamed to '{new_name}'.")
