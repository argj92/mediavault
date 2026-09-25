from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import dedupe, db, recycle_bin, scanner, suggestions, sync
from .config import AppConfig, ConfigError, RootConfig, load_config

app = typer.Typer(help="MediaVault — cross-machine movie/TV library manager.")
root_app = typer.Typer(help="Manage tracked folders (roots) on this machine.")
pair_app = typer.Typer(help="Manage sync pairs between roots.")
trash_app = typer.Typer(help="Inspect and clean up the OS trash / recycle bin.")
app.add_typer(root_app, name="root")
app.add_typer(pair_app, name="pair")
app.add_typer(trash_app, name="trash")

console = Console()


def _load(config_path: Optional[Path] = None) -> AppConfig:
    try:
        return load_config(config_path)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


def _human(n) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


@app.command()
def init(config_path: Path = typer.Option(None, help="Where to write config.yaml (default: ./config.yaml)")):
    """Create a fresh config.yaml for this machine and initialize its local index."""
    target = config_path or (Path.cwd() / "config.yaml")
    if target.exists():
        console.print(f"[yellow]{target} already exists — leaving it alone.[/yellow]")
    else:
        target.write_text(
            "machine: {}\n"
            "db_path: ~/.local/share/mediavault/mediavault.db\n"
            "hash_algo: blake3\n"
            "tmdb_api_key_env: TMDB_API_KEY\n"
            "scan_interval_minutes: 15\n"
            "video_extensions: [\".mkv\", \".mp4\", \".m4v\", \".avi\", \".mov\", \".wmv\", \".ts\"]\n".format(
                __import__("socket").gethostname()
            )
        )
        console.print(f"[green]Wrote {target}[/green]")

    config = _load(target)
    with db.connect(config.db_path) as conn:
        db.init_db(conn)
    console.print(f"[green]Initialized index at {config.db_path}[/green]")
    console.print("Next: [bold]mediavault root add[/bold] to track your first folder, or run [bold]mediavault web[/bold] and use the UI.")


# --------------------------------------------------------------------- roots

@root_app.command("add")
def root_add(
    label: str,
    path: Path,
    role: str = typer.Option("primary", help="primary | backup | icloud | inbox"),
    mode: str = typer.Option("mirror", help="mirror | subset | watch"),
):
    config = _load()
    with db.connect(config.db_path) as conn:
        db.init_db(conn)
        db.upsert_root(conn, label, str(path.expanduser().resolve()), role, mode)
    console.print(f"[green]Added root '{label}' ({role}/{mode}) -> {path}[/green]")


@root_app.command("list")
def root_list():
    config = _load()
    with db.connect(config.db_path) as conn:
        db.init_db(conn)
        rows = db.list_roots(conn)
    table = Table("Label", "Role", "Mode", "Path", "Enabled")
    for r in rows:
        table.add_row(r["label"], r["role"], r["mode"], r["path"], "yes" if r["enabled"] else "no")
    console.print(table)


@root_app.command("remove")
def root_remove(label: str, yes: bool = typer.Option(False, "--yes", help="skip confirmation")):
    if not yes:
        typer.confirm(f"Remove root '{label}' from tracking? (files on disk are untouched)", abort=True)
    config = _load()
    with db.connect(config.db_path) as conn:
        db.remove_root(conn, label)
    console.print(f"[green]Removed '{label}'.[/green]")


@root_app.command("scan")
def root_scan(label: Optional[str] = typer.Argument(None, help="omit to scan every enabled root")):
    config = _load()
    with db.connect(config.db_path) as conn:
        db.init_db(conn)
        rows = [db.get_root(conn, label)] if label else db.list_roots(conn, enabled_only=True)
        for row in rows:
            if row is None:
                console.print(f"[red]No such root '{label}'[/red]")
                continue
            rc = RootConfig.from_row(row)
            try:
                stats = scanner.scan_root(conn, rc, hash_algo=config.hash_algo)
                console.print(f"[green]{rc.label}[/green]: {stats}")
            except FileNotFoundError as exc:
                console.print(f"[yellow]{rc.label}: {exc}[/yellow]")


# ---------------------------------------------------------------------- pairs

@pair_app.command("add")
def pair_add(root_a: str, root_b: str):
    config = _load()
    with db.connect(config.db_path) as conn:
        pair_id = db.add_sync_pair(conn, root_a, root_b)
    console.print(f"[green]Watching pair #{pair_id}: {root_a} <-> {root_b}[/green]")


@pair_app.command("list")
def pair_list():
    config = _load()
    with db.connect(config.db_path) as conn:
        rows = db.list_sync_pairs(conn)
    table = Table("ID", "A", "B", "Status", "Enabled")
    for r in rows:
        table.add_row(str(r["id"]), r["root_a"], r["root_b"], r["status"], "yes" if r["enabled"] else "no")
    console.print(table)


@pair_app.command("remove")
def pair_remove(pair_id: int):
    config = _load()
    with db.connect(config.db_path) as conn:
        db.remove_sync_pair(conn, pair_id)
    console.print(f"[green]Removed pair #{pair_id}.[/green]")


@pair_app.command("sync")
def pair_sync(pair_id: int, apply_deletes: bool = typer.Option(False, help="also apply deletion candidates (irreversible)")):
    config = _load()
    with db.connect(config.db_path) as conn:
        pair = db.get_sync_pair(conn, pair_id)
        if not pair:
            console.print(f"[red]No such pair #{pair_id}[/red]")
            raise typer.Exit(1)
        root_a = RootConfig.from_row(db.get_root(conn, pair["root_a"]))
        root_b = RootConfig.from_row(db.get_root(conn, pair["root_b"]))
        plan = sync.plan_sync(conn, root_a, root_b)
        console.print(plan.summary())
        if apply_deletes and (plan.delete_from_a or plan.delete_from_b):
            typer.confirm(
                f"Really delete {len(plan.delete_from_a) + len(plan.delete_from_b)} file(s) permanently?",
                abort=True,
            )
        results = sync.execute_sync(root_a, root_b, plan, apply_deletes=apply_deletes)
        console.print(results)
        scanner.scan_root(conn, root_a, hash_algo=config.hash_algo)
        scanner.scan_root(conn, root_b, hash_algo=config.hash_algo)


# ------------------------------------------------------------------ dedupe

@app.command(name="dedupe")
def dedupe_cmd():
    """Show duplicate files across every tracked root."""
    config = _load()
    with db.connect(config.db_path) as conn:
        groups = dedupe.find_duplicates(conn)
    table = Table("Copies", "Size each", "Wasted", "Paths")
    for g in groups:
        paths = "\n".join(f"{f['root_label']}:{f['rel_path']}" for f in g["files"])
        table.add_row(str(g["count"]), _human(g["total_size"] // g["count"]), _human(g["wasted_bytes"]), paths)
    console.print(table)
    console.print(f"Total reclaimable: {_human(dedupe.total_wasted_bytes(groups))}")


@app.command(name="suggestions")
def suggestions_cmd():
    """Show untracked media sitting in inbox/watch folders."""
    config = _load()
    with db.connect(config.db_path) as conn:
        items = suggestions.untracked_media(conn, config.video_extensions)
    table = Table("Root", "Path", "Size", "Status")
    for i in items:
        table.add_row(i["root_label"], i["rel_path"], _human(i["size"]), "in library already" if i["already_in_library"] else "not tracked")
    console.print(table)


@app.command()
def icloud(label: str, top: int = 25):
    """Show storage breakdown + largest local files for an icloud-role root."""
    from . import cloud

    config = _load()
    with db.connect(config.db_path) as conn:
        row = db.get_root(conn, label)
    if not row:
        console.print(f"[red]No such root '{label}'[/red]")
        raise typer.Exit(1)
    path = Path(row["path"])
    console.print(cloud.storage_summary(path))
    table = Table("Path", "Size")
    for rel, size in cloud.largest_downloaded_files(path, top_n=top):
        table.add_row(rel, _human(size))
    console.print(table)


# ---------------------------------------------------------------------- trash

@trash_app.command("list")
def trash_list(top: int = 50):
    items = recycle_bin.list_trash_items(top_n=top)
    table = Table("Path", "Size", "Location")
    for i in items:
        table.add_row(str(i.path), _human(i.size), i.source)
    console.print(table)
    console.print(f"Total in trash: {_human(recycle_bin.total_trash_bytes())}")


@trash_app.command("empty")
def trash_empty(
    paths: list[str] = typer.Argument(None, help="specific paths to empty; omit to empty everything listed"),
    yes: bool = typer.Option(False, "--yes", help="skip confirmation"),
):
    targets = paths or [str(i.path) for i in recycle_bin.list_trash_items(top_n=10_000)]
    if not targets:
        console.print("Trash is already empty.")
        return
    if not yes:
        typer.confirm(f"Permanently delete {len(targets)} item(s) from trash?", abort=True)
    result = recycle_bin.empty_items(targets, confirm=True)
    console.print(f"[green]Deleted {len(result['deleted'])} item(s).[/green]")
    if result["errors"]:
        console.print(f"[red]{len(result['errors'])} error(s): {result['errors']}[/red]")


# ------------------------------------------------------------------ web / gui

@app.command()
def web(
    host: str = "127.0.0.1",
    port: int = 8420,
    open_browser: bool = typer.Option(True, help="open the dashboard in your default browser"),
):
    """Run the local web UI (also usable from another device on your LAN)."""
    import threading
    import uvicorn

    from .web.app import app as fastapi_app, configure

    config = _load()
    configure(config)

    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}")).start()

    uvicorn.run(fastapi_app, host=host, port=port, log_level="info")


@app.command()
def gui():
    """Run the desktop GUI — the same UI as `mediavault web`, in a native window."""
    from .gui.app import run_gui

    config = _load()
    run_gui(config)


if __name__ == "__main__":
    app()
