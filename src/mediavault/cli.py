from __future__ import annotations

import sys
import time
import webbrowser
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import catalog, dedupe, db, protection, recycle_bin, scanner, suggestions, sync
from .config import AppConfig, ConfigError, RootConfig, load_config

app = typer.Typer(help="MediaVault — cross-machine movie/TV library manager.")
root_app = typer.Typer(help="Manage tracked folders (roots) on this machine.")
trash_app = typer.Typer(help="Inspect and clean up the OS trash / recycle bin.")
service_app = typer.Typer(help="Run mediavault as an always-on background service (launchd/systemd).")
catalog_app = typer.Typer(help="Export/import portable per-machine catalogs for cross-machine protection tracking.")
app.add_typer(root_app, name="root")
app.add_typer(trash_app, name="trash")
app.add_typer(service_app, name="service")
app.add_typer(catalog_app, name="catalog")

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
        if role == "primary" and db.count_primary_roots(conn, exclude_label=label) > 0:
            console.print(
                "[red]There's already a primary root — that's the single basis every other "
                "root syncs against, so only one is supported. Use --role backup/icloud/inbox, "
                "or change the existing primary's role first.[/red]"
            )
            raise typer.Exit(1)
        db.upsert_root(conn, label, str(path.expanduser().resolve()), role, mode)
    console.print(f"[green]Added root '{label}' ({role}/{mode}) -> {path}[/green]")


@root_app.command("edit")
def root_edit(
    label: str,
    role: str = typer.Option(None, help="primary | backup | icloud | inbox"),
    mode: str = typer.Option(None, help="mirror | subset | watch"),
):
    """Change a root's role/mode in place -- without losing its indexed
    files the way `remove` + `add` would."""
    if role is None and mode is None:
        console.print("[yellow]Nothing to change — pass --role and/or --mode.[/yellow]")
        raise typer.Exit(1)
    config = _load()
    with db.connect(config.db_path) as conn:
        row = db.get_root(conn, label)
        if row is None:
            console.print(f"[red]No such root '{label}'[/red]")
            raise typer.Exit(1)
        new_role = role or row["role"]
        new_mode = mode or row["mode"]
        if new_role == "primary" and db.count_primary_roots(conn, exclude_label=label) > 0:
            console.print(
                "[red]There's already a primary root — that's the single basis every other "
                "root syncs against, so only one is supported. Change the existing primary's "
                "role first.[/red]"
            )
            raise typer.Exit(1)
        db.update_root_role_mode(conn, label, new_role, new_mode)
    console.print(f"[green]Updated '{label}' to {new_role}/{new_mode}.[/green]")


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


# ----------------------------------------------------------------------- sync
# Single-basis model: one root is marked `primary`; every other mirror/subset
# root syncs against it automatically — nothing to manually wire up.

@app.command(name="sync-status")
def sync_status_cmd():
    """Show each root's sync status against the primary."""
    config = _load()
    with db.connect(config.db_path) as conn:
        statuses = sync.sync_status_for_all(conn)
    if not statuses:
        console.print("[yellow]No primary root configured yet — add one with `mediavault root add ... --role primary`.[/yellow]")
        return
    table = Table("Root", "Status", "To copy", "Needs review")
    for s in statuses:
        plan = s["plan"]
        table.add_row(
            s["root"].label,
            "out of sync" if plan.out_of_sync else "in sync",
            str(len(plan.copy_to_a) + len(plan.copy_to_b)),
            str(len(plan.delete_from_a) + len(plan.delete_from_b) + len(plan.conflicts)),
        )
    console.print(table)


@app.command(name="sync")
def sync_cmd(
    label: Optional[str] = typer.Argument(None, help="omit to sync every root against the primary"),
    apply_deletes: bool = typer.Option(False, help="also apply deletion candidates (irreversible)"),
):
    """Sync one root (or all of them) against the primary root. Copies are
    always applied; deletions only with --apply-deletes, after confirming."""
    config = _load()
    with db.connect(config.db_path) as conn:
        primary_row = db.get_primary_root(conn)
        if primary_row is None:
            console.print("[red]No primary root configured — add one with `mediavault root add ... --role primary`.[/red]")
            raise typer.Exit(1)
        primary = RootConfig.from_row(primary_row)

        targets = [db.get_root(conn, label)] if label else db.syncable_roots(conn, exclude_label=primary.label)
        for row in targets:
            if row is None:
                console.print(f"[red]No such root '{label}'[/red]")
                continue
            other = RootConfig.from_row(row)
            plan = sync.plan_sync(conn, primary, other)
            console.print(f"[bold]{other.label}[/bold]: {plan.summary()}")
            if apply_deletes and (plan.delete_from_a or plan.delete_from_b):
                typer.confirm(
                    f"Really delete {len(plan.delete_from_a) + len(plan.delete_from_b)} file(s) "
                    f"permanently for '{other.label}'?",
                    abort=True,
                )
            results = sync.execute_sync(primary, other, plan, apply_deletes=apply_deletes)
            console.print(results)
            scanner.scan_root(conn, primary, hash_algo=config.hash_algo)
            scanner.scan_root(conn, other, hash_algo=config.hash_algo)


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


# ------------------------------------------------------------------- protection

@app.command(name="protection")
def protection_cmd(top: int = 25):
    """Show files backed by fewer than 2 independent mirror-mode copies —
    across this machine, and any other machine whose catalog you've
    imported (see `mediavault catalog`)."""
    config = _load()
    with db.connect(config.db_path) as conn:
        report = protection.compute_protection(conn, config.machine)
    console.print(
        f"{report['covered']}/{report['total_groups']} file(s) have 2+ protective copies. "
        f"Known machines: {', '.join(report['known_machines']) or '(none imported)'}"
    )
    table = Table("Size", "Copies", "Locations")
    for g in report["at_risk"][:top]:
        locs = "\n".join(f"{loc['machine']}:{loc['root_label']}/{loc['rel_path']}" for loc in g["locations"])
        table.add_row(_human(g["size"]), str(g["protective_count"]), locs)
    console.print(table)
    if not report["at_risk"]:
        console.print("[green]Nothing at risk — every file has 2+ protective copies.[/green]")


# --------------------------------------------------------------------- catalog

@catalog_app.command("export")
def catalog_export(out: Path = typer.Option(None, help="default: ./<machine>-catalog.json")):
    """Dump this machine's index (hash/size/path/role, no file contents) as
    portable JSON another machine can import for cross-machine protection
    tracking — no network access, move the file yourself."""
    config = _load()
    out = out or Path.cwd() / f"{config.machine}-catalog.json"
    with db.connect(config.db_path) as conn:
        n = catalog.export_catalog(conn, config.machine, out)
    console.print(f"[green]Wrote {n} file(s) to {out}[/green]")


@catalog_app.command("import")
def catalog_import(path: Path):
    """Import another machine's exported catalog (see `catalog export`)."""
    config = _load()
    try:
        data = catalog.load_catalog_file(path)
        with db.connect(config.db_path) as conn:
            n = catalog.import_catalog(conn, data, config.machine)
    except catalog.CatalogError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Imported {n} file(s) from '{data['machine']}'.[/green]")


@catalog_app.command("list")
def catalog_list():
    """Show which other machines' catalogs are currently imported."""
    config = _load()
    with db.connect(config.db_path) as conn:
        rows = db.list_remote_catalogs(conn)
    table = Table("Machine", "Files", "Imported", "Exported at")
    for r in rows:
        table.add_row(r["machine"], str(r["file_count"]), time.strftime("%Y-%m-%d %H:%M", time.localtime(r["imported_at"])), r["exported_at"] or "-")
    console.print(table)


@catalog_app.command("remove")
def catalog_remove(machine: str):
    """Remove a previously imported catalog."""
    config = _load()
    with db.connect(config.db_path) as conn:
        db.remove_remote_catalog(conn, machine)
    console.print(f"[green]Removed catalog for '{machine}'.[/green]")


# ---------------------------------------------------------------------- trash

@trash_app.command("list")
def trash_list(top: int = 50):
    scan = recycle_bin.scan_trash(top_n=top)
    table = Table("Path", "Size", "Location")
    for i in scan["items"]:
        table.add_row(str(i.path), _human(i.size), i.source)
    console.print(table)
    if not scan["accurate"]:
        console.print(
            f"[yellow]Warning: {len(scan['access_errors'])} location(s) couldn't be read "
            f"(permission denied) — the total below is a floor, not the real size.[/yellow]"
        )
        for err in scan["access_errors"]:
            console.print(f"  [yellow]{err.location}: {err.error}[/yellow]")
        console.print(
            "[yellow]On macOS: System Settings -> Privacy & Security -> Full Disk Access -> "
            "enable it for the app/terminal running mediavault, then re-run.[/yellow]"
        )
    console.print(f"Total in trash: {_human(scan['total_bytes'])}{'' if scan['accurate'] else ' (incomplete)'}")


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


def web_entry() -> None:
    """Console-script entry point for the standalone `mediavault-web` command
    (same as `mediavault web`, so it still takes --host/--port/--open-browser)."""
    typer.run(web)


def gui_entry() -> None:
    """Console-script entry point for the standalone `mediavault-gui` command
    (same as `mediavault gui`)."""
    typer.run(gui)


# ------------------------------------------------------------------- service

@service_app.command("install")
def service_install(port: int = 8420):
    """Install mediavault as an always-on background service (launchd on
    macOS, systemd --user on Linux) so scanning/duplicate/sync-status
    notifications keep happening even with no UI open, and it survives
    reboots. Safe to re-run; `service uninstall` fully reverses it."""
    from . import service

    config = _load()
    bin_path = Path(sys.executable).parent / "mediavault"
    if not bin_path.exists():
        console.print(f"[red]Could not find the mediavault executable next to {sys.executable}[/red]")
        raise typer.Exit(1)
    msg = service.install(bin_path.resolve(), Path.cwd(), config.config_path.resolve(), port=port)
    console.print(f"[green]{msg}[/green]")


@service_app.command("uninstall")
def service_uninstall():
    """Stop and remove the background service."""
    from . import service

    console.print(service.uninstall())


@service_app.command("status")
def service_status():
    """Show whether the background service is installed/running."""
    from . import service

    console.print(service.status())


if __name__ == "__main__":
    app()
