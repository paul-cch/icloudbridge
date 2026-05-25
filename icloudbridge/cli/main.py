"""Command-line interface for iCloudBridge."""

import asyncio
import sys
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from icloudbridge import __version__
from icloudbridge.core.config import load_config
from icloudbridge.core.photos_export_engine import ExportConfig, PhotoExportEngine
from icloudbridge.core.photos_sync import PhotoSyncEngine
from icloudbridge.core.reminders_sync import RemindersSyncEngine
from icloudbridge.core.rich_notes_export import RichNotesExporter
from icloudbridge.core.sync import NotesSyncEngine
from icloudbridge.core.notion_reminders_readonly import (
    CreateApplePartialFailure,
    CreateNotionPartialFailure,
    assert_expected_proof_plan,
    assert_proof_ready_plan,
    build_apple_snapshot,
    build_bidirectional_update_plan,
    build_notion_snapshot,
    build_proof_mutation,
    build_readonly_match_report,
    build_readonly_sync_plan,
    execute_create_apple_plan,
    execute_create_notion_plan,
    execute_update_apple_plan,
    execute_update_notion_plan,
)
from icloudbridge.sources.reminders.notion_adapter import (
    DEFAULT_NOTION_API_VERSION,
    DEFAULT_TASKS_DATA_SOURCE_ID,
    DISPOSABLE_APPLE_TO_NOTION_TITLE,
    DISPOSABLE_NOTION_TO_APPLE_TITLE,
    NotionAuthError,
    NotionSchemaReport,
    NotionTasksAdapter,
    PropertyCheck,
    page_apple_sync_id,
    page_notes,
    page_title,
    load_notion_token,
)
from icloudbridge.utils.logging import setup_logging
from icloudbridge.utils.photos_db import PhotosDB
from icloudbridge.utils.settings_db import get_config_path, set_config_path

# Create Typer app
app = typer.Typer(
    name="icloudbridge",
    help="Synchronize Apple Notes & Reminders to NextCloud, CalDAV, and local folders",
    add_completion=False,
)

# Create console for rich output
console = Console()


@app.callback()
def main(
    ctx: typer.Context,
    config_file: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to configuration file",
        exists=True,
        dir_okay=False,
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        "-l",
        help="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    ),
) -> None:
    """iCloudBridge - Sync Apple Notes & Reminders."""
    # Store config in context for subcommands
    ctx.ensure_object(dict)
    effective_config_path = config_file
    if effective_config_path is None:
        stored_path = get_config_path()
        if stored_path:
            effective_config_path = stored_path

    cfg = load_config(effective_config_path)
    ctx.obj["config"] = cfg

    if cfg.general.config_file:
        set_config_path(cfg.general.config_file)

    # Set up logging based on config or CLI arg
    if log_level == "INFO" and ctx.obj["config"].general.log_level != "INFO":
        log_level = ctx.obj["config"].general.log_level
    setup_logging(ctx.obj["config"], level_name=log_level)


@app.command()
def version() -> None:
    """Show version information."""
    import platform

    table = Table(title="iCloudBridge Version Information")
    table.add_column("Property", style="cyan", no_wrap=True)
    table.add_column("Value", style="green")

    table.add_row("Version", __version__)
    table.add_row("Python", platform.python_version())
    table.add_row("Platform", platform.platform())
    table.add_row("Architecture", platform.machine())

    console.print(table)


@app.command()
def config(
    ctx: typer.Context,
    show: bool = typer.Option(
        False,
        "--show",
        "-s",
        help="Show current configuration",
    ),
    init: bool = typer.Option(
        False,
        "--init",
        "-i",
        help="Create a default configuration file",
    ),
) -> None:
    """Manage configuration."""
    cfg = ctx.obj["config"]

    if init:
        # Create default config file
        config_path = cfg.default_config_path

        if config_path.exists():
            console.print(f"[yellow]Config file already exists:[/yellow] {config_path}")
            overwrite = typer.confirm("Overwrite existing config?")
            if not overwrite:
                console.print("[dim]Config creation cancelled[/dim]")
                raise typer.Exit(0)

        # Create config with example values
        try:
            cfg.save_to_file(config_path)
            set_config_path(config_path)
            console.print(f"[green]✓ Config file created:[/green] {config_path}")
            console.print("\n[cyan]Example configuration:[/cyan]")
            console.print(f"[dim]{config_path}[/dim]\n")
            console.print("[yellow]Edit this file to configure iCloudBridge.[/yellow]")
            console.print("[dim]See the documentation for all available options.[/dim]")
        except ImportError:
            console.print("[red]Error: tomli_w not installed[/red]")
            console.print("[dim]Install with: pip install tomli-w[/dim]")
            raise typer.Exit(1)

        return

    if show:
        table = Table(title="iCloudBridge Configuration")
        table.add_column("Setting", style="cyan", no_wrap=True)
        table.add_column("Value", style="green")

        # General settings
        table.add_row("Data Directory", str(cfg.general.data_dir))
        table.add_row("Config File", str(cfg.general.config_file or "Not set"))
        table.add_row("Log Level", cfg.general.log_level)

        # Notes settings
        table.add_row("", "")  # Separator
        table.add_row("[bold]Notes[/bold]", "")
        table.add_row("Enabled", "✓" if cfg.notes.enabled else "✗")
        table.add_row(
            "Remote Folder",
            str(cfg.notes.remote_folder) if cfg.notes.remote_folder else "Not set",
        )

        # Reminders settings
        table.add_row("", "")  # Separator
        table.add_row("[bold]Reminders[/bold]", "")
        table.add_row("Enabled", "✓" if cfg.reminders.enabled else "✗")
        table.add_row(
            "CalDAV URL",
            cfg.reminders.caldav_url if cfg.reminders.caldav_url else "Not set",
        )
        table.add_row(
            "CalDAV Username",
            cfg.reminders.caldav_username if cfg.reminders.caldav_username else "Not set",
        )

        # Photos settings
        table.add_row("", "")
        table.add_row("[bold]Photos[/bold]", "")
        table.add_row("Enabled", "✓" if cfg.photos.enabled else "✗")
        table.add_row("Default Album", cfg.photos.default_album)
        table.add_row("Sources", str(len(cfg.photos.sources)))

        console.print(table)
    else:
        console.print(
            f"[yellow]Configuration file:[/yellow] {cfg.general.config_file or 'Not set'}"
        )
        console.print(
            f"[yellow]Data directory:[/yellow] {cfg.general.data_dir}",
        )
        console.print(
            "\n[dim]Use --show to display full configuration[/dim]",
        )
        console.print(
            "[dim]Use --init to create a default config file[/dim]",
        )


@app.command()
def photos(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview new imports without sending anything to Photos"),
    initial_scan: bool = typer.Option(
        False,
        "--initial-scan",
        help="Build the photo cache without importing into Photos (used after changing sources)",
    ),
    source: list[str] = typer.Option(None, "--source", "-s", help="Limit sync to specific source keys"),
):
    """Synchronize configured photo watch folders into Apple Photos."""

    cfg = ctx.obj["config"]
    if not cfg.photos.enabled:
        console.print("[red]Photos sync is disabled in the configuration.[/red]")
        raise typer.Exit(1)

    engine = PhotoSyncEngine(cfg.photos, cfg.general.data_dir)
    stats = asyncio.run(engine.sync(sources=source or None, dry_run=dry_run, initial_scan=initial_scan))

    table = Table(title="Photo Sync Results")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Sources scanned", ", ".join(source or engine.scanner.available_sources()) or "None")
    table.add_row("Files discovered", str(stats.get("discovered", 0)))
    table.add_row("Skipped (fast-path)", str(stats.get("skipped_by_mtime", 0)))
    table.add_row("Skipped (in Photos)", str(stats.get("skipped_existing", 0)))
    table.add_row("New assets", str(stats.get("new_assets", 0)))
    table.add_row("Imported", str(stats.get("imported", 0)))
    if stats.get("dry_run"):
        table.add_row("Mode", "Dry run (no changes)")
    elif stats.get("initial_scan"):
        table.add_row("Mode", "Initial scan (cache only)")

    albums = stats.get("albums")
    if albums:
        table.add_row("Album breakdown", ", ".join(f"{name} ({count})" for name, count in albums.items()))

    console.print(table)


@app.command("photos-export")
def photos_export(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview export without uploading to NextCloud"),
    full_library: bool = typer.Option(False, "--full-library", help="Export entire library, ignoring baseline date"),
    album: str = typer.Option(None, "--album", "-a", help="Only export photos from this album"),
    since: str = typer.Option(None, "--since", help="Only export photos created after this date (YYYY-MM-DD)"),
    set_baseline: bool = typer.Option(False, "--set-baseline-only", help="Set baseline to now without exporting"),
):
    """Export photos from Apple Photos to NextCloud.

    Default behavior (going forward): Only export photos added after the baseline date.
    First run sets the baseline; subsequent runs export only new photos.

    Use --full-library to export your entire library (may take a while for large libraries).
    Use --dry-run to preview what would be exported without uploading.
    """
    from datetime import datetime

    cfg = ctx.obj["config"]

    if not cfg.photos.enabled:
        console.print("[red]Photos sync is disabled in the configuration.[/red]")
        raise typer.Exit(1)

    if cfg.photos.sync_mode not in ("export", "bidirectional"):
        console.print(
            f"[red]Photo export requires sync_mode='export' or 'bidirectional', "
            f"got '{cfg.photos.sync_mode}'[/red]"
        )
        console.print("[dim]Set photos.sync_mode in your config file or via the settings page.[/dim]")
        raise typer.Exit(1)

    export_cfg = cfg.photos.export

    # Determine export folder (defaults to first import source path)
    export_folder = export_cfg.export_folder
    if not export_folder:
        if cfg.photos.sources:
            first_source = next(iter(cfg.photos.sources.values()))
            export_folder = first_source.path
        else:
            console.print("[red]No export folder configured and no import sources available.[/red]")
            console.print("[dim]Configure a photos source folder or set photos.export.export_folder in your config.[/dim]")
            raise typer.Exit(1)

    # Create export engine for local file copy
    export_config = ExportConfig(
        export_folder=Path(export_folder),
        organize_by=export_cfg.organize_by,
    )

    db = PhotosDB(cfg.general.data_dir / "photos.db")

    async def run_export():
        await db.initialize()
        engine = PhotoExportEngine(config=export_config, db=db)
        await engine.initialize()

        try:
            # Handle --set-baseline-only
            if set_baseline:
                await engine.set_baseline()
                export_state = await db.get_export_state()
                baseline_date = None
                if export_state and export_state.get("baseline_date"):
                    baseline_date = datetime.fromtimestamp(export_state["baseline_date"]).isoformat()
                console.print(f"[green]Baseline set to: {baseline_date}[/green]")
                console.print("[dim]Future exports will only include photos added after this time.[/dim]")
                return None

            # Parse since date
            since_date = None
            if since:
                try:
                    since_date = datetime.fromisoformat(since)
                except ValueError:
                    console.print(f"[red]Invalid date format: {since}[/red]")
                    console.print("[dim]Use ISO format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS[/dim]")
                    raise typer.Exit(1)

            return await engine.export(
                full_library=full_library,
                since_date=since_date,
                album_filter=album,
                dry_run=dry_run,
            )
        finally:
            await engine.cleanup()

    stats = asyncio.run(run_export())

    if stats is None:
        # set-baseline-only mode
        return

    table = Table(title="Photo Export Results")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    if stats.get("baseline_set"):
        table.add_row("Baseline", "Set to current time")
        table.add_row("Message", stats.get("message", "Run export again to export new photos"))
    else:
        if dry_run:
            table.add_row("Mode", "Dry run (no changes)")
            table.add_row("Would export", str(stats.get("would_export", 0)))
        else:
            table.add_row("Exported", str(stats.get("exported", 0)))

        table.add_row("Skipped (before baseline)", str(stats.get("skipped_before_baseline", 0)))
        table.add_row("Skipped (already exported)", str(stats.get("skipped_already_exported", 0)))
        table.add_row("Skipped (from NextCloud)", str(stats.get("skipped_imported_from_nextcloud", 0)))
        table.add_row("Errors", str(stats.get("errors", 0)))

    console.print(table)

    # Show preview for dry run
    if dry_run and stats.get("preview"):
        preview_table = Table(title="Files to Export (first 50)")
        preview_table.add_column("Filename", style="cyan")
        preview_table.add_column("Remote Path", style="green")
        preview_table.add_column("Created", style="yellow")

        for item in stats["preview"]:
            preview_table.add_row(
                item.get("filename", "?"),
                item.get("remote_path", "?"),
                item.get("created", "?")[:10],  # Just the date part
            )

        console.print(preview_table)


@app.command("db-paths")
def db_paths(ctx: typer.Context) -> None:
    """Show the database files used by the CLI."""
    cfg = ctx.obj["config"]

    table = Table(title="Database Locations")
    table.add_column("Database", style="cyan", no_wrap=True)
    table.add_column("Path", style="green")
    table.add_column("Status", style="magenta")

    entries = [
        (
            "Notes",
            cfg.notes_db_path,
            "Apple Notes ↔ Markdown sync mappings",
        ),
        (
            "Reminders",
            cfg.reminders_db_path,
            "Apple Reminders ↔ CalDAV sync mappings",
        ),
        (
            "Passwords",
            cfg.passwords_db_path,
            "Passwords sync metadata",
        ),
    ]

    for name, path, description in entries:
        exists = path.exists()
        status = "✓ exists" if exists else "✗ not created yet"
        table.add_row(name, f"{path}\n[dim]{description}[/dim]", status)

    console.print(table)


@app.command()
def health(ctx: typer.Context) -> None:
    """Check application health and dependencies."""
    cfg = ctx.obj["config"]

    console.print("[bold]Health Check[/bold]\n")

    # Check data directory
    if cfg.general.data_dir.exists():
        console.print("✓ Data directory exists", style="green")
    else:
        console.print("✗ Data directory does not exist", style="red")

    # Check databases
    notes_db = cfg.notes_db_path
    if notes_db.exists():
        console.print(f"✓ Notes DB ready: {notes_db}", style="green")
    else:
        console.print(f"ℹ Notes DB not initialized: {notes_db}", style="yellow")

    reminders_db = cfg.reminders_db_path
    if reminders_db.exists():
        console.print(f"✓ Reminders DB ready: {reminders_db}", style="green")
    else:
        console.print(f"ℹ Reminders DB not initialized: {reminders_db}", style="yellow")

    passwords_db = cfg.passwords_db_path
    if passwords_db.exists():
        console.print(f"✓ Passwords DB ready: {passwords_db}", style="green")
    else:
        console.print(f"ℹ Passwords DB not initialized: {passwords_db}", style="yellow")

    # Check notes remote folder
    if cfg.notes.enabled:
        if cfg.notes.remote_folder and cfg.notes.remote_folder.exists():
            console.print("✓ Notes remote folder exists", style="green")
        elif cfg.notes.remote_folder:
            console.print("✗ Notes remote folder does not exist", style="red")
        else:
            console.print("ℹ Notes remote folder not configured", style="yellow")

    # Check reminders CalDAV
    if cfg.reminders.enabled:
        if cfg.reminders.caldav_url:
            console.print("✓ CalDAV URL configured", style="green")
        else:
            console.print("ℹ CalDAV URL not configured", style="yellow")

    console.print("\n[dim]Status: Ready[/dim]")


# Notes subcommand group
notes_app = typer.Typer(help="Manage notes synchronization")
app.add_typer(notes_app, name="notes")


@notes_app.command("sync")
def notes_sync(
    ctx: typer.Context,
    folder: Optional[str] = typer.Option(None, "--folder", "-f", help="Sync specific folder only"),
    mode: str = typer.Option(
        "bidirectional",
        "--mode",
        "-m",
        help="Sync direction: 'import' (Markdown → Apple Notes), 'export' (Apple Notes → Markdown), or 'bidirectional' (both ways)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Preview changes without applying them"),
    skip_deletions: bool = typer.Option(False, "--skip-deletions", help="Skip all deletion operations"),
    deletion_threshold: int = typer.Option(5, "--deletion-threshold", help="Max deletions before confirmation (use -1 to disable)"),
    rich_notes: bool = typer.Option(
        False,
        "--rich-notes/--no-rich-notes",
        help="After syncing, export rich notes snapshot into RichNotes/",
    ),
    shortcut_push: Optional[bool] = typer.Option(
        None,
        "--shortcut-push/--classic-push",
        help="Use the Shortcut pipeline (default) or legacy AppleScript pipeline when pushing markdown back to Apple Notes",
    ),
) -> None:
    """Synchronize notes between Apple Notes and markdown files."""
    cfg = ctx.obj["config"]

    # Validate mode
    valid_modes = {"import", "export", "bidirectional"}
    if mode not in valid_modes:
        console.print(f"[red]Invalid mode '{mode}'. Must be one of: {', '.join(valid_modes)}[/red]")
        raise typer.Exit(1)

    # Check if notes sync is enabled
    if not cfg.notes.enabled:
        console.print("[red]Notes sync is not enabled in configuration[/red]")
        console.print("[dim]Enable it in your config file or use environment variables[/dim]")
        raise typer.Exit(1)

    # Check if remote folder is configured
    if not cfg.notes.remote_folder:
        console.print("[red]Notes remote folder is not configured[/red]")
        console.print("[dim]Set ICLOUDBRIDGE_NOTES_REMOTE_FOLDER in your config[/dim]")
        raise typer.Exit(1)

    if dry_run:
        console.print("[cyan]DRY RUN MODE: Previewing changes only[/cyan]\n")

    # Show mode if not bidirectional
    if mode != "bidirectional":
        mode_label = {
            "import": "IMPORT ONLY (Markdown → Apple Notes)",
            "export": "EXPORT ONLY (Apple Notes → Markdown)"
        }
        console.print(f"[yellow]{mode_label[mode]}[/yellow]\n")

    async def run_sync():
        def print_pending_notes(notes_list: list | None, indent: str = "  ") -> None:
            if not notes_list:
                return
            console.print(f"{indent}[yellow]Pending edits detected:[/yellow]")
            for note in notes_list:
                title = note.get("title") or Path(note.get("remote_path", "")).name or "Untitled"
                folder_label = note.get("folder") or "Unknown folder"
                console.print(f"{indent}  • {title} [dim](folder: {folder_label})[/dim]")

        # Initialize sync engine
        prefer_shortcuts = shortcut_push if shortcut_push is not None else cfg.notes.use_shortcuts_for_push

        sync_engine = NotesSyncEngine(
            markdown_base_path=cfg.notes.remote_folder,
            db_path=cfg.notes_db_path,
            prefer_shortcuts=prefer_shortcuts,
        )
        await sync_engine.initialize()

        # Automatically migrate any root-level notes to "Notes" folder
        if not dry_run:
            migrated = await sync_engine.migrate_root_notes_to_folder()
            if migrated > 0:
                console.print(f"[yellow]Migrated {migrated} root-level note(s) to 'Notes' folder[/yellow]\n")

        # Check if folder mappings are configured
        using_mappings = bool(cfg.notes.folder_mappings) and not folder

        if using_mappings:
            # Use selective sync with folder mappings
            console.print(f"[cyan]Using folder mappings ({len(cfg.notes.folder_mappings)} configured)[/cyan]\n")

            # Convert FolderMapping objects to dict format
            folder_mappings_dict = {}
            for apple_folder, mapping_obj in cfg.notes.folder_mappings.items():
                folder_mappings_dict[apple_folder] = {
                    "markdown_folder": mapping_obj.markdown_folder,
                    "mode": mapping_obj.mode
                }

            # Run sync with mappings
            folder_results = await sync_engine.sync_with_mappings(
                folder_mappings=folder_mappings_dict,
                dry_run=dry_run,
                skip_deletions=skip_deletions,
                deletion_threshold=deletion_threshold,
            )

            # Convert results to match expected format
            total_stats = {
                "created_local": 0,
                "created_remote": 0,
                "updated_local": 0,
                "updated_remote": 0,
                "deleted_local": 0,
                "deleted_remote": 0,
                "unchanged": 0,
                "would_delete_local": 0,
                "would_delete_remote": 0,
                "pending_local_notes": [],
            }

            for folder_name, stats in folder_results.items():
                console.print(f"[bold]Folder:[/bold] {folder_name}")

                if "error" in stats:
                    console.print(f"  [red]✗ Failed: {stats['error']}[/red]")
                    continue

                # Aggregate stats
                total_stats["created_local"] += stats.get("created_local", 0)
                total_stats["created_remote"] += stats.get("created_remote", 0)
                total_stats["updated_local"] += stats.get("updated_local", 0)
                total_stats["updated_remote"] += stats.get("updated_remote", 0)
                total_stats["deleted_local"] += stats.get("deleted_local", 0)
                total_stats["deleted_remote"] += stats.get("deleted_remote", 0)
                total_stats["unchanged"] += stats.get("unchanged", 0)
                total_stats["would_delete_local"] += stats.get("would_delete_local", 0)
                total_stats["would_delete_remote"] += stats.get("would_delete_remote", 0)
                if stats.get("pending_local_notes"):
                    total_stats["pending_local_notes"].extend(stats["pending_local_notes"])

                # Show folder stats
                if dry_run:
                    if any(stats.get(k, 0) > 0 for k in ["created_local", "created_remote", "updated_local", "updated_remote", "would_delete_local", "would_delete_remote"]):
                        console.print(
                            f"  [yellow]Preview:[/yellow] "
                            f"{stats.get('created_remote', 0)} would create, "
                            f"{stats.get('updated_remote', 0)} would update, "
                            f"{stats.get('would_delete_remote', 0)} would delete (remote)"
                        )
                        console.print(
                            f"  [yellow]Preview:[/yellow] "
                            f"{stats.get('created_local', 0)} would create, "
                            f"{stats.get('updated_local', 0)} would update, "
                            f"{stats.get('would_delete_local', 0)} would delete (local)"
                        )
                    else:
                        console.print(f"  [dim]No changes needed ({stats.get('unchanged', 0)} unchanged)[/dim]")
                elif any(stats.get(k, 0) > 0 for k in ["created_local", "created_remote", "updated_local", "updated_remote", "deleted_local", "deleted_remote"]):
                    console.print(
                        f"  [green]✓[/green] "
                        f"{stats.get('created_remote', 0)} created, "
                        f"{stats.get('updated_remote', 0)} updated, "
                        f"{stats.get('deleted_remote', 0)} deleted (remote)"
                    )
                    console.print(
                        f"  [green]✓[/green] "
                        f"{stats.get('created_local', 0)} created, "
                        f"{stats.get('updated_local', 0)} updated, "
                        f"{stats.get('deleted_local', 0)} deleted (local)"
                    )
                else:
                    console.print(f"  [dim]No changes needed ({stats.get('unchanged', 0)} unchanged)[/dim]")

                print_pending_notes(stats.get("pending_local_notes"))

            if total_stats["pending_local_notes"]:
                pending_count = len(total_stats["pending_local_notes"])
                console.print(
                    f"\n[yellow]{pending_count} note{'s' if pending_count != 1 else ''} appear to be mid-edit in Apple Notes. They'll be retried on the next sync.[/yellow]\n"
                )

        else:
            # Auto 1:1 sync or single folder sync
            if folder:
                folders_to_sync = [folder]
                console.print(f"[cyan]Syncing folder:[/cyan] {folder}\n")
            else:
                console.print("[cyan]Fetching folders from Apple Notes...[/cyan]")
                all_folders = await sync_engine.list_folders()
                folders_to_sync = [f["name"] for f in all_folders]
                console.print(f"[green]Found {len(folders_to_sync)} folders[/green]\n")

            # Sync each folder
            total_stats = {
                "created_local": 0,
                "created_remote": 0,
                "updated_local": 0,
                "updated_remote": 0,
                "deleted_local": 0,
                "deleted_remote": 0,
                "unchanged": 0,
                "would_delete_local": 0,
                "would_delete_remote": 0,
                "pending_local_notes": [],
            }

            for folder_name in folders_to_sync:
                try:
                    console.print(f"[bold]Syncing folder:[/bold] {folder_name}")
                    stats = await sync_engine.sync_folder(
                        folder_name,
                        folder_name,
                        dry_run=dry_run,
                        skip_deletions=skip_deletions,
                        deletion_threshold=deletion_threshold,
                        sync_mode=mode,
                    )
                except RuntimeError as e:
                    console.print(f"[red]Error syncing {folder_name}: {e}[/red]")
                    logger.exception("Folder sync failed")
                    continue

                total_stats["created_local"] += stats.get("created_local", 0)
                total_stats["created_remote"] += stats.get("created_remote", 0)
                total_stats["updated_local"] += stats.get("updated_local", 0)
                total_stats["updated_remote"] += stats.get("updated_remote", 0)
                total_stats["deleted_local"] += stats.get("deleted_local", 0)
                total_stats["deleted_remote"] += stats.get("deleted_remote", 0)
                total_stats["unchanged"] += stats.get("unchanged", 0)
                total_stats["would_delete_local"] += stats.get("would_delete_local", 0)
                total_stats["would_delete_remote"] += stats.get("would_delete_remote", 0)
                if stats.get("pending_local_notes"):
                    total_stats["pending_local_notes"].extend(stats["pending_local_notes"])

                # Show folder stats
                numeric_keys = [
                    "created_local",
                    "created_remote",
                    "updated_local",
                    "updated_remote",
                    "deleted_local",
                    "deleted_remote",
                ]

                if dry_run:
                    if any(stats.get(k, 0) > 0 for k in numeric_keys) or stats["would_delete_local"] > 0 or stats["would_delete_remote"] > 0:
                        console.print(
                            f"  [yellow]Preview:[/yellow] "
                            f"{stats['created_remote']} would create, "
                            f"{stats['updated_remote']} would update, "
                            f"{stats['would_delete_remote']} would delete "
                            f"(remote)"
                        )
                        console.print(
                            f"  [yellow]Preview:[/yellow] "
                            f"{stats['created_local']} would create, "
                            f"{stats['updated_local']} would update, "
                            f"{stats['would_delete_local']} would delete "
                            f"(local)"
                        )
                    else:
                        console.print(f"  [dim]No changes needed ({stats['unchanged']} unchanged)[/dim]")
                elif any(stats.get(k, 0) > 0 for k in numeric_keys):
                    console.print(
                        f"  [green]✓[/green] "
                        f"{stats['created_remote']} created, "
                        f"{stats['updated_remote']} updated, "
                        f"{stats['deleted_remote']} deleted "
                        f"(remote)"
                    )
                    console.print(
                        f"  [green]✓[/green] "
                        f"{stats['created_local']} created, "
                        f"{stats['updated_local']} updated, "
                        f"{stats['deleted_local']} deleted "
                        f"(local)"
                    )
                else:
                    console.print(f"  [dim]No changes needed ({stats['unchanged']} unchanged)[/dim]")

                print_pending_notes(stats.get("pending_local_notes"))

            if total_stats["pending_local_notes"]:
                pending_count = len(total_stats["pending_local_notes"])
                console.print(
                    f"\n[yellow]{pending_count} note{'s' if pending_count != 1 else ''} appear to be mid-edit in Apple Notes. They'll be retried on the next sync.[/yellow]\n"
                )

        # Show summary
        if dry_run:
            console.print("\n[bold]Dry Run Summary (Preview Only)[/bold]")
        else:
            console.print("\n[bold]Sync Summary[/bold]")

        table = Table()
        table.add_column("Operation", style="cyan")
        table.add_column("Local (Apple Notes)", style="green", justify="right")
        table.add_column("Remote (Markdown)", style="blue", justify="right")

        if dry_run:
            table.add_row("Would Create", str(total_stats["created_local"]), str(total_stats["created_remote"]))
            table.add_row("Would Update", str(total_stats["updated_local"]), str(total_stats["updated_remote"]))
            table.add_row("Would Delete", str(total_stats["would_delete_local"]), str(total_stats["would_delete_remote"]))
            table.add_row("Unchanged", str(total_stats["unchanged"]), str(total_stats["unchanged"]))
        else:
            table.add_row("Created", str(total_stats["created_local"]), str(total_stats["created_remote"]))
            table.add_row("Updated", str(total_stats["updated_local"]), str(total_stats["updated_remote"]))
            table.add_row("Deleted", str(total_stats["deleted_local"]), str(total_stats["deleted_remote"]))
            table.add_row("Unchanged", str(total_stats["unchanged"]), str(total_stats["unchanged"]))

        console.print(table)

        if sync_engine.shortcut_calls:
            console.print("\n[dim]Shortcut invocations this run:[/dim]")
            for entry in sync_engine.shortcut_calls:
                temp_info = entry.get("temp_path") or "-"
                console.print(
                    f"  - {entry['shortcut']} (folder='{entry['folder']}', note='{entry['title']}', temp={temp_info})",
                    style="dim",
                )

        if dry_run:
            console.print("\n[yellow]This was a dry run. No changes were made.[/yellow]")
            console.print("[dim]Run without --dry-run to apply these changes.[/dim]")

    # Run async sync
    try:
        asyncio.run(run_sync())
    except Exception as e:
        console.print(f"[red]Sync failed: {e}[/red]")
        logging.exception("Sync operation failed")
        raise typer.Exit(1) from e

    if rich_notes:
        try:
            exporter = RichNotesExporter(cfg.notes_db_path, cfg.notes.remote_folder)
            exporter.export(dry_run=dry_run)
            if dry_run:
                console.print(
                    "[yellow]RichNotes export skipped (dry run). Run without --dry-run to generate files.[/yellow]"
                )
            else:
                console.print("[green]✓ RichNotes export complete[/green]")
        except Exception as exc:  # pragma: no cover - filesystem heavy
            console.print(f"[red]RichNotes export failed: {exc}[/red]")
            logging.exception("RichNotes export failed")


@notes_app.command("list")
def notes_list(ctx: typer.Context) -> None:
    """List all Apple Notes folders."""
    cfg = ctx.obj["config"]

    async def run_list():
        # Initialize sync engine
        sync_engine = NotesSyncEngine(
            markdown_base_path=cfg.notes.remote_folder or Path("/tmp"),
            db_path=cfg.notes_db_path,
        )
        await sync_engine.initialize()

        # Get folders
        console.print("[cyan]Fetching folders from Apple Notes...[/cyan]\n")
        folders = await sync_engine.list_folders()

        if not folders:
            console.print("[yellow]No folders found in Apple Notes[/yellow]")
            return

        # Display folders in a table
        table = Table(title="Apple Notes Folders")
        table.add_column("Folder Name", style="cyan", no_wrap=True)
        table.add_column("UUID", style="dim")

        for folder in folders:
            table.add_row(folder["name"], folder["uuid"])

        console.print(table)
        console.print(f"\n[dim]Total: {len(folders)} folders[/dim]")

    # Run async list
    try:
        asyncio.run(run_list())
    except Exception as e:
        console.print(f"[red]Failed to list folders: {e}[/red]")
        logging.exception("List operation failed")
        raise typer.Exit(1) from e


@notes_app.command("status")
def notes_status(ctx: typer.Context) -> None:
    """Show notes synchronization status."""
    cfg = ctx.obj["config"]

    # Check if notes sync is enabled
    if not cfg.notes.enabled:
        console.print("[red]Notes sync is not enabled in configuration[/red]")
        return

    async def run_status():
        # Initialize sync engine
        sync_engine = NotesSyncEngine(
            markdown_base_path=cfg.notes.remote_folder or Path("/tmp"),
            db_path=cfg.notes_db_path,
        )
        await sync_engine.initialize()

        # Get sync status
        status = await sync_engine.get_sync_status()

        # Display status
        table = Table(title="Notes Sync Status")
        table.add_column("Property", style="cyan", no_wrap=True)
        table.add_column("Value", style="green")

        table.add_row("Total Synced Notes", str(status["total_mappings"]))
        table.add_row("Remote Folder", str(cfg.notes.remote_folder))
        table.add_row("Database", str(cfg.notes_db_path))

        console.print(table)

        if status["total_mappings"] == 0:
            console.print("\n[yellow]No notes have been synced yet[/yellow]")
            console.print("[dim]Run 'icloudbridge notes sync' to start syncing[/dim]")

    # Run async status
    try:
        asyncio.run(run_status())
    except Exception as e:
        console.print(f"[red]Failed to get status: {e}[/red]")
        logging.exception("Status operation failed")
        raise typer.Exit(1) from e


@notes_app.command("reset")
def notes_reset(
    ctx: typer.Context,
    confirm: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt",
    ),
) -> None:
    """Reset the sync database (clears all note mappings)."""
    cfg = ctx.obj["config"]

    # Confirm with user
    if not confirm:
        console.print("[yellow]⚠ Warning: This will clear all note sync mappings![/yellow]")
        console.print("[dim]Your notes will NOT be deleted, but the sync engine will treat")
        console.print("everything as 'new' on the next sync.[/dim]\n")

        response = typer.confirm("Are you sure you want to reset the database?")
        if not response:
            console.print("[dim]Reset cancelled[/dim]")
            raise typer.Exit(0)

    async def run_reset():
        # Initialize sync engine
        sync_engine = NotesSyncEngine(
            markdown_base_path=cfg.notes.remote_folder or Path("/tmp"),
            db_path=cfg.notes_db_path,
        )
        await sync_engine.initialize()

        # Clear all mappings
        await sync_engine.reset_database()

        console.print("[green]✓ Database reset successfully[/green]")
        console.print("[dim]Run 'icloudbridge notes sync' to start fresh[/dim]")

    # Run async reset
    try:
        asyncio.run(run_reset())
    except Exception as e:
        console.print(f"[red]Failed to reset database: {e}[/red]")
        logging.exception("Reset operation failed")
        raise typer.Exit(1) from e


# Reminders subcommand group
reminders_app = typer.Typer(help="Manage reminders synchronization")
app.add_typer(reminders_app, name="reminders")


def _format_check(check: PropertyCheck) -> str:
    """Format a Notion property check for CLI output."""
    if check.ok:
        return "✓"
    if not check.present:
        return "missing"
    return f"got {check.actual_type}"


def _print_notion_schema_report(report: NotionSchemaReport) -> None:
    """Print a Notion Tasks preflight schema report."""
    status = "[green]PASS[/green]" if report.ok else "[red]FAIL[/red]"
    console.print(
        Panel(
            f"{status}\n"
            f"[bold]Data source:[/bold] {report.title}\n"
            f"[bold]Data source ID:[/bold] {report.data_source_id}\n"
            f"[bold]Database ID:[/bold] {report.database_id or 'unknown'}\n"
            f"[bold]API version:[/bold] {report.api_version}",
            title="Notion Tasks Schema",
        )
    )

    table = Table(title="Required Properties")
    table.add_column("Property", style="cyan")
    table.add_column("Expected", style="green")
    table.add_column("Result", style="magenta")
    for check in report.required:
        table.add_row(check.name, check.expected_type, _format_check(check))
    console.print(table)

    identity_table = Table(title="Sync Identity Properties")
    identity_table.add_column("Property", style="cyan")
    identity_table.add_column("Expected", style="green")
    identity_table.add_column("Result", style="magenta")
    for check in report.identity:
        identity_table.add_row(check.name, check.expected_type, _format_check(check))
    console.print(identity_table)

    if report.optional:
        optional_table = Table(title="Optional Properties")
        optional_table.add_column("Property", style="cyan")
        optional_table.add_column("Expected", style="green")
        optional_table.add_column("Result", style="magenta")
        for check in report.optional:
            optional_table.add_row(check.name, check.expected_type, _format_check(check))
        console.print(optional_table)

    if report.status_options:
        console.print("[cyan]Status options:[/cyan] " + ", ".join(report.status_options))
    if report.source_options:
        console.print("[cyan]Source options:[/cyan] " + ", ".join(report.source_options))
    if report.area_options:
        console.print("[cyan]Area options:[/cyan] " + ", ".join(report.area_options))

    for warning in report.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")
    for error in report.errors:
        console.print(f"[red]Error:[/red] {error}")


@reminders_app.command("notion-preflight")
def reminders_notion_preflight(
    data_source_id: str = typer.Option(
        DEFAULT_TASKS_DATA_SOURCE_ID,
        "--data-source-id",
        help="Notion Tasks data source ID to inspect",
    ),
    apple_calendar: str = typer.Option(
        "Notion Sync Test",
        "--apple-calendar",
        "-a",
        help="Apple Reminders list to check for read access",
    ),
    notion_token: Optional[str] = typer.Option(
        None,
        "--notion-token",
        envvar="NOTION_API_TOKEN",
        help="Notion API token. Defaults to NOTION_API_TOKEN or ntn file auth.",
    ),
    api_version: str = typer.Option(
        DEFAULT_NOTION_API_VERSION,
        "--notion-version",
        help="Notion API version header",
    ),
    skip_apple: bool = typer.Option(
        False,
        "--skip-apple",
        help="Skip Apple Reminders access/list checks",
    ),
) -> None:
    """Run read-only preflight checks for future Notion Tasks ↔ Apple Reminders sync."""

    async def run_preflight() -> bool:
        ok = True

        try:
            token = load_notion_token(notion_token)
        except NotionAuthError as exc:
            console.print(f"[red]Notion auth failed:[/red] {exc}")
            return False

        adapter = NotionTasksAdapter(token=token, api_version=api_version)
        try:
            report = await adapter.preflight_schema(data_source_id)
        except httpx.HTTPStatusError as exc:
            response = exc.response
            console.print(
                f"[red]Notion schema check failed:[/red] "
                f"HTTP {response.status_code} {response.reason_phrase}"
            )
            ok = False
        except httpx.HTTPError as exc:
            console.print(f"[red]Notion schema check failed:[/red] {exc}")
            ok = False
        else:
            _print_notion_schema_report(report)
            ok = report.ok

        if skip_apple:
            console.print("[yellow]Apple Reminders check skipped.[/yellow]")
            return ok

        from icloudbridge.sources.reminders.eventkit import RemindersAdapter

        try:
            reminders_adapter = RemindersAdapter()
            access_granted = await reminders_adapter.request_access()
            if not access_granted:
                console.print("[red]Apple Reminders access was not granted.[/red]")
                return False

            calendars = await reminders_adapter.list_calendars()
            calendar_names = [cal.title for cal in calendars]
            target = next((cal for cal in calendars if cal.title == apple_calendar), None)

            table = Table(title="Apple Reminders Access")
            table.add_column("Check", style="cyan")
            table.add_column("Result", style="green")
            table.add_row("Access granted", "✓")
            table.add_row("Lists found", str(len(calendars)))
            table.add_row(
                f"Target list '{apple_calendar}'",
                "✓" if target else "missing",
            )
            console.print(table)

            if not target:
                ok = False
                console.print(
                    "[yellow]Available lists:[/yellow] "
                    + (", ".join(calendar_names) if calendar_names else "none")
                )
                console.print(
                    f"[red]Error:[/red] Create an Apple Reminders list named "
                    f"'{apple_calendar}' or pass --apple-calendar."
                )
            else:
                reminders = await reminders_adapter.get_reminders(calendar_id=target.uuid)
                console.print(
                    f"[green]✓[/green] Read {len(reminders)} reminder(s) from "
                    f"'{apple_calendar}' without writes."
                )

        except Exception as exc:
            console.print(f"[red]Apple Reminders check failed:[/red] {exc}")
            ok = False

        if ok:
            console.print(
                "\n[green]Milestone 0 preflight passed for read-only checks.[/green]\n"
                "[dim]Write capability is intentionally unverified until a disposable "
                "Notion test row or test data source is provided.[/dim]"
            )
        else:
            console.print(
                "\n[red]Milestone 0 preflight failed.[/red]\n"
                "[dim]No sync writes were attempted. Fix the reported schema/access "
                "issues before implementing sync writes.[/dim]"
            )

        return ok

    passed = asyncio.run(run_preflight())
    if not passed:
        raise typer.Exit(1)


@reminders_app.command("notion-proof")
def reminders_notion_proof(
    data_source_id: str = typer.Option(
        DEFAULT_TASKS_DATA_SOURCE_ID,
        "--data-source-id",
        help="Notion Tasks data source ID to use for the disposable proof row",
    ),
    apple_calendar: str = typer.Option(
        "Notion Sync Test",
        "--apple-calendar",
        "-a",
        help="Apple Reminders list to use for the disposable proof reminder",
    ),
    notion_token: Optional[str] = typer.Option(
        None,
        "--notion-token",
        envvar="NOTION_API_TOKEN",
        help="Notion API token. Defaults to NOTION_API_TOKEN or ntn file auth.",
    ),
    api_version: str = typer.Option(
        DEFAULT_NOTION_API_VERSION,
        "--notion-version",
        help="Notion API version header",
    ),
) -> None:
    """Prove disposable Notion and Apple Reminders write/read capability."""

    async def run_proof() -> bool:
        from datetime import datetime, timezone
        from uuid import uuid4

        from icloudbridge.sources.reminders.eventkit import RemindersAdapter

        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

        try:
            token = load_notion_token(notion_token)
        except NotionAuthError as exc:
            console.print(f"[red]Notion auth failed:[/red] {exc}")
            return False

        notion = NotionTasksAdapter(token=token, api_version=api_version)

        try:
            report = await notion.preflight_schema(data_source_id)
            if not report.ok:
                _print_notion_schema_report(report)
                return False

            notion_pages = await notion.query_tasks_by_title(
                data_source_id, DISPOSABLE_NOTION_TO_APPLE_TITLE
            )
            if len(notion_pages) > 1:
                console.print(
                    "[red]Refusing to continue:[/red] multiple disposable Notion rows "
                    f"named {DISPOSABLE_NOTION_TO_APPLE_TITLE!r} were found."
                )
                return False

            if notion_pages:
                notion_page = notion_pages[0]
                sync_id = page_apple_sync_id(notion_page)
                if not sync_id:
                    console.print(
                        "[red]Refusing to update disposable Notion row without "
                        "Apple Sync ID.[/red]"
                    )
                    return False
                notion_action = "found"
            else:
                sync_id = f"apple-reminders-proof:{uuid4()}"
                notion_page = await notion.create_disposable_task(
                    data_source_id=data_source_id,
                    apple_sync_id=sync_id,
                    notes=f"Disposable sync proof row created at {timestamp}.",
                )
                notion_action = "created"

            notion_page_id = notion_page["id"]
            notion_note = f"Disposable sync proof updated at {timestamp}. Sync ID: {sync_id}"
            await notion.update_page_notes(notion_page_id, notion_note)
            notion_page = await notion.retrieve_page(notion_page_id)

            if page_title(notion_page) != DISPOSABLE_NOTION_TO_APPLE_TITLE:
                console.print("[red]Notion re-read verification failed: title changed.[/red]")
                return False
            if page_apple_sync_id(notion_page) != sync_id:
                console.print("[red]Notion re-read verification failed: sync ID mismatch.[/red]")
                return False
            if page_notes(notion_page) != notion_note:
                console.print("[red]Notion re-read verification failed: notes mismatch.[/red]")
                return False

        except httpx.HTTPStatusError as exc:
            response = exc.response
            console.print(
                f"[red]Notion proof failed:[/red] "
                f"HTTP {response.status_code} {response.reason_phrase}"
            )
            return False
        except httpx.HTTPError as exc:
            console.print(f"[red]Notion proof failed:[/red] {exc}")
            return False

        try:
            reminders = RemindersAdapter()
            if not await reminders.request_access():
                console.print("[red]Apple Reminders access was not granted.[/red]")
                return False

            calendars = await reminders.list_calendars()
            target_calendar = next((cal for cal in calendars if cal.title == apple_calendar), None)
            if not target_calendar:
                console.print(
                    f"[red]Apple Reminders list not found:[/red] {apple_calendar!r}"
                )
                return False

            apple_items = [
                reminder
                for reminder in await reminders.get_reminders(calendar_id=target_calendar.uuid)
                if reminder.title == DISPOSABLE_APPLE_TO_NOTION_TITLE
            ]
            if len(apple_items) > 1:
                console.print(
                    "[red]Refusing to continue:[/red] multiple disposable Apple reminders "
                    f"named {DISPOSABLE_APPLE_TO_NOTION_TITLE!r} were found in "
                    f"{apple_calendar!r}."
                )
                return False

            if apple_items:
                apple_item = apple_items[0]
                apple_action = "found"
            else:
                apple_item = await reminders.create_reminder(
                    calendar_id=target_calendar.uuid,
                    title=DISPOSABLE_APPLE_TO_NOTION_TITLE,
                    notes=f"Disposable sync proof reminder created at {timestamp}.",
                    completed=False,
                )
                apple_action = "created"

            apple_note = (
                f"Disposable sync proof updated at {timestamp}. "
                f"Notion page: {notion_page_id}"
            )
            await reminders.update_reminder(apple_item.uuid, notes=apple_note)
            apple_items = [
                reminder
                for reminder in await reminders.get_reminders(calendar_id=target_calendar.uuid)
                if reminder.title == DISPOSABLE_APPLE_TO_NOTION_TITLE
            ]

            if len(apple_items) != 1:
                console.print("[red]Apple re-read verification failed: item count mismatch.[/red]")
                return False
            apple_item = apple_items[0]
            if apple_item.notes != apple_note:
                console.print("[red]Apple re-read verification failed: notes mismatch.[/red]")
                return False

        except Exception as exc:
            console.print(f"[red]Apple proof failed:[/red] {exc}")
            return False

        table = Table(title="Disposable Notion ↔ Apple Proof")
        table.add_column("Surface", style="cyan")
        table.add_column("Action", style="green")
        table.add_column("Verified Item")
        table.add_row("Notion Tasks", notion_action, DISPOSABLE_NOTION_TO_APPLE_TITLE)
        table.add_row("Apple Reminders", apple_action, DISPOSABLE_APPLE_TO_NOTION_TITLE)
        console.print(table)
        console.print(
            "\n[green]Disposable write proof passed.[/green]\n"
            "[dim]Only the exact [SYNC TEST] Notion row and exact [SYNC TEST] "
            "Apple reminder were created or updated.[/dim]"
        )
        return True

    passed = asyncio.run(run_proof())
    if not passed:
        raise typer.Exit(1)


@reminders_app.command("notion-readonly")
def reminders_notion_readonly(
    data_source_id: str = typer.Option(
        DEFAULT_TASKS_DATA_SOURCE_ID,
        "--data-source-id",
        help="Notion Tasks data source ID to read",
    ),
    apple_calendar: str = typer.Option(
        "Notion Sync Test",
        "--apple-calendar",
        "-a",
        help="Apple Reminders list to compare",
    ),
    notion_token: Optional[str] = typer.Option(
        None,
        "--notion-token",
        envvar="NOTION_API_TOKEN",
        help="Notion API token. Defaults to NOTION_API_TOKEN or ntn file auth.",
    ),
    api_version: str = typer.Option(
        DEFAULT_NOTION_API_VERSION,
        "--notion-version",
        help="Notion API version header",
    ),
    notion_page_size: int = typer.Option(
        100,
        "--notion-page-size",
        min=1,
        max=100,
        help="Notion page size for the enrolled-row read",
    ),
) -> None:
    """Read Notion and Apple reminders, then print read-only match buckets."""

    def date_label(value) -> str:
        if value is None:
            return ""
        return value.isoformat()

    async def run_readonly() -> bool:
        from icloudbridge.sources.reminders.eventkit import RemindersAdapter

        try:
            token = load_notion_token(notion_token)
        except NotionAuthError as exc:
            console.print(f"[red]Notion auth failed:[/red] {exc}")
            return False

        notion = NotionTasksAdapter(token=token, api_version=api_version)

        try:
            report = await notion.preflight_schema(data_source_id)
            if not report.ok:
                _print_notion_schema_report(report)
                return False
            notion_tasks = await notion.query_tasks_with_apple_sync_id(
                data_source_id=data_source_id,
                page_size=notion_page_size,
            )
        except httpx.HTTPStatusError as exc:
            response = exc.response
            console.print(
                f"[red]Notion read-only proof failed:[/red] "
                f"HTTP {response.status_code} {response.reason_phrase}"
            )
            return False
        except httpx.HTTPError as exc:
            console.print(f"[red]Notion read-only proof failed:[/red] {exc}")
            return False

        try:
            reminders = RemindersAdapter()
            if not await reminders.request_access():
                console.print("[red]Apple Reminders access was not granted.[/red]")
                return False

            calendars = await reminders.list_calendars()
            target_calendar = next((cal for cal in calendars if cal.title == apple_calendar), None)
            if not target_calendar:
                console.print(
                    f"[red]Apple Reminders list not found:[/red] {apple_calendar!r}"
                )
                return False
            apple_reminders = await reminders.get_reminders(calendar_id=target_calendar.uuid)
        except Exception as exc:
            console.print(f"[red]Apple read-only proof failed:[/red] {exc}")
            return False

        match_report = build_readonly_match_report(notion_tasks, apple_reminders)

        summary = Table(title="Milestone 1 Read-Only Summary")
        summary.add_column("Bucket", style="cyan")
        summary.add_column("Count", style="green", justify="right")
        summary.add_row("Notion enrolled rows", str(len(notion_tasks)))
        summary.add_row(f"Apple reminders in {apple_calendar}", str(len(apple_reminders)))
        summary.add_row("Matched", str(len(match_report.matched)))
        summary.add_row("Notion-only", str(len(match_report.notion_only)))
        summary.add_row("Apple-only", str(len(match_report.apple_only)))
        console.print(summary)

        matched_table = Table(title="Matched")
        matched_table.add_column("Reason", style="cyan")
        matched_table.add_column("Notion")
        matched_table.add_column("Apple")
        matched_table.add_column("Status")
        for item in match_report.matched:
            matched_table.add_row(
                item.reason,
                item.notion_task.title,
                item.apple_reminder.title,
                item.notion_task.status,
            )
        console.print(matched_table)

        notion_table = Table(title="Notion-Only")
        notion_table.add_column("Title")
        notion_table.add_column("Status", style="cyan")
        notion_table.add_column("Apple Sync ID")
        notion_table.add_column("Due")
        for task in match_report.notion_only:
            notion_table.add_row(
                task.title,
                task.status,
                task.apple_sync_id or "",
                date_label(task.due_date),
            )
        console.print(notion_table)

        apple_table = Table(title="Apple-Only")
        apple_table.add_column("Title")
        apple_table.add_column("Completed", style="cyan")
        apple_table.add_column("Apple ID")
        apple_table.add_column("Due")
        for reminder in match_report.apple_only:
            apple_table.add_row(
                reminder.title,
                "yes" if reminder.completed else "no",
                reminder.uuid,
                date_label(reminder.due_date),
            )
        console.print(apple_table)

        console.print(
            "\n[green]Milestone 1 read-only proof completed.[/green]\n"
            "[dim]No Notion pages or Apple reminders were created, updated, completed, "
            "cancelled, or deleted.[/dim]"
        )
        return True

    passed = asyncio.run(run_readonly())
    if not passed:
        raise typer.Exit(1)


@reminders_app.command("notion-plan")
def reminders_notion_plan(
    data_source_id: str = typer.Option(
        DEFAULT_TASKS_DATA_SOURCE_ID,
        "--data-source-id",
        help="Notion Tasks data source ID to read",
    ),
    apple_calendar: str = typer.Option(
        "Notion Sync Test",
        "--apple-calendar",
        "-a",
        help="Apple Reminders list to compare",
    ),
    notion_token: Optional[str] = typer.Option(
        None,
        "--notion-token",
        envvar="NOTION_API_TOKEN",
        help="Notion API token. Defaults to NOTION_API_TOKEN or ntn file auth.",
    ),
    api_version: str = typer.Option(
        DEFAULT_NOTION_API_VERSION,
        "--notion-version",
        help="Notion API version header",
    ),
    notion_page_size: int = typer.Option(
        100,
        "--notion-page-size",
        min=1,
        max=100,
        help="Notion page size for the enrolled-row read",
    ),
) -> None:
    """Print dry-run Notion ↔ Apple reminder actions without writes."""

    def date_label(value) -> str:
        if value is None:
            return ""
        return value.isoformat()

    async def run_plan() -> bool:
        from icloudbridge.sources.reminders.eventkit import RemindersAdapter

        try:
            token = load_notion_token(notion_token)
        except NotionAuthError as exc:
            console.print(f"[red]Notion auth failed:[/red] {exc}")
            return False

        notion = NotionTasksAdapter(token=token, api_version=api_version)

        try:
            report = await notion.preflight_schema(data_source_id)
            if not report.ok:
                _print_notion_schema_report(report)
                return False
            notion_tasks = await notion.query_tasks_with_apple_sync_id(
                data_source_id=data_source_id,
                page_size=notion_page_size,
            )
        except httpx.HTTPStatusError as exc:
            response = exc.response
            console.print(
                f"[red]Notion planning read failed:[/red] "
                f"HTTP {response.status_code} {response.reason_phrase}"
            )
            return False
        except httpx.HTTPError as exc:
            console.print(f"[red]Notion planning read failed:[/red] {exc}")
            return False

        try:
            reminders = RemindersAdapter()
            if not await reminders.request_access():
                console.print("[red]Apple Reminders access was not granted.[/red]")
                return False

            calendars = await reminders.list_calendars()
            target_calendar = next((cal for cal in calendars if cal.title == apple_calendar), None)
            if not target_calendar:
                console.print(
                    f"[red]Apple Reminders list not found:[/red] {apple_calendar!r}"
                )
                return False
            apple_reminders = await reminders.get_reminders(calendar_id=target_calendar.uuid)
        except Exception as exc:
            console.print(f"[red]Apple planning read failed:[/red] {exc}")
            return False

        if apple_calendar != "Notion Sync Test":
            console.print(
                f"[yellow]Warning:[/yellow] planning against non-test list "
                f"{apple_calendar!r}; this command is still read-only."
            )

        match_report = build_readonly_match_report(notion_tasks, apple_reminders)
        sync_plan = build_readonly_sync_plan(match_report)

        summary = Table(title="Milestone 2 Dry-Run Plan Summary")
        summary.add_column("Action", style="cyan")
        summary.add_column("Count", style="green", justify="right")
        for kind in ("NOOP", "CREATE_APPLE", "CREATE_NOTION"):
            summary.add_row(kind, str(sync_plan.counts[kind]))
        console.print(summary)

        actions_table = Table(title="Planned Actions")
        actions_table.add_column("Action", style="cyan")
        actions_table.add_column("Direction")
        actions_table.add_column("Title")
        actions_table.add_column("Status/Completed")
        actions_table.add_column("Due")
        actions_table.add_column("Source ID")
        for action in sync_plan.actions:
            actions_table.add_row(
                action.kind,
                action.direction,
                action.title,
                action.status,
                date_label(action.due_date),
                action.source_id,
            )
        console.print(actions_table)

        console.print(
            "\n[green]Milestone 2 dry-run plan completed.[/green]\n"
            "[dim]No Notion pages or Apple reminders were created, updated, completed, "
            "cancelled, or deleted.[/dim]"
        )
        return True

    passed = asyncio.run(run_plan())
    if not passed:
        raise typer.Exit(1)


@reminders_app.command("notion-create-apple")
def reminders_notion_create_apple(
    ctx: typer.Context,
    data_source_id: str = typer.Option(
        DEFAULT_TASKS_DATA_SOURCE_ID,
        "--data-source-id",
        help="Notion Tasks data source ID to read",
    ),
    apple_calendar: str = typer.Option(
        "Notion Sync Test",
        "--apple-calendar",
        "-a",
        help="Apple Reminders list to create into",
    ),
    notion_token: Optional[str] = typer.Option(
        None,
        "--notion-token",
        envvar="NOTION_API_TOKEN",
        help="Notion API token. Defaults to NOTION_API_TOKEN or ntn file auth.",
    ),
    api_version: str = typer.Option(
        DEFAULT_NOTION_API_VERSION,
        "--notion-version",
        help="Notion API version header",
    ),
    notion_page_size: int = typer.Option(
        100,
        "--notion-page-size",
        min=1,
        max=100,
        help="Notion page size for the enrolled-row read",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually create Apple reminders for CREATE_APPLE actions",
    ),
    allow_multiple_test_creates: bool = typer.Option(
        False,
        "--allow-multiple-test-creates",
        help="Allow more than one CREATE_APPLE action in the Notion Sync Test list",
    ),
) -> None:
    """Create Apple reminders from Notion test-slice rows, gated by --apply."""

    def date_label(value) -> str:
        if value is None:
            return ""
        return value.isoformat()

    def print_sync_plan(title: str, sync_plan) -> None:
        summary = Table(title=title)
        summary.add_column("Action", style="cyan")
        summary.add_column("Count", style="green", justify="right")
        for kind in ("NOOP", "CREATE_APPLE", "CREATE_NOTION"):
            summary.add_row(kind, str(sync_plan.counts[kind]))
        console.print(summary)

        actions_table = Table(title="Planned Actions")
        actions_table.add_column("Action", style="cyan")
        actions_table.add_column("Direction")
        actions_table.add_column("Title")
        actions_table.add_column("Status/Completed")
        actions_table.add_column("Due")
        actions_table.add_column("Source ID")
        for action in sync_plan.actions:
            actions_table.add_row(
                action.kind,
                action.direction,
                action.title,
                action.status,
                date_label(action.due_date),
                action.source_id,
            )
        console.print(actions_table)

    async def read_plan(notion, reminders):
        report = await notion.preflight_schema(data_source_id)
        if not report.ok:
            _print_notion_schema_report(report)
            return None, None

        notion_tasks = await notion.query_tasks_with_apple_sync_id(
            data_source_id=data_source_id,
            page_size=notion_page_size,
        )

        if not await reminders.request_access():
            console.print("[red]Apple Reminders access was not granted.[/red]")
            return None, None

        calendars = await reminders.list_calendars()
        target_calendar = next((cal for cal in calendars if cal.title == apple_calendar), None)
        if not target_calendar:
            console.print(f"[red]Apple Reminders list not found:[/red] {apple_calendar!r}")
            return None, None

        apple_reminders = await reminders.get_reminders(calendar_id=target_calendar.uuid)
        match_report = build_readonly_match_report(notion_tasks, apple_reminders)
        return target_calendar, build_readonly_sync_plan(match_report)

    async def run_create_apple() -> bool:
        from icloudbridge.sources.reminders.eventkit import RemindersAdapter
        from icloudbridge.utils.db import NotionRemindersDB

        if apple_calendar != "Notion Sync Test":
            console.print(
                "[red]Refusing to run:[/red] Milestone 3 writes are limited to "
                "'Notion Sync Test'."
            )
            return False

        try:
            token = load_notion_token(notion_token)
        except NotionAuthError as exc:
            console.print(f"[red]Notion auth failed:[/red] {exc}")
            return False

        notion = NotionTasksAdapter(token=token, api_version=api_version)
        reminders = RemindersAdapter()

        try:
            target_calendar, sync_plan = await read_plan(notion, reminders)
            if target_calendar is None or sync_plan is None:
                return False
            print_sync_plan("Milestone 3 Notion → Apple Plan", sync_plan)

            if not apply:
                console.print(
                    "\n[yellow]Dry run only.[/yellow] Pass --apply to create Apple reminders.\n"
                    "[dim]No Notion pages or Apple reminders were created, updated, "
                    "completed, cancelled, or deleted.[/dim]"
                )
                return True

            db = NotionRemindersDB(ctx.obj["config"].reminders_db_path)
            await db.initialize()
            result = await execute_create_apple_plan(
                sync_plan,
                apple_calendar_name=apple_calendar,
                apple_calendar_id=target_calendar.uuid,
                reminders_adapter=reminders,
                notion_adapter=notion,
                db=db,
                allow_multiple_test_creates=allow_multiple_test_creates,
            )

            result_table = Table(title="Milestone 3 Apply Result")
            result_table.add_column("Metric", style="cyan")
            result_table.add_column("Count", style="green", justify="right")
            result_table.add_row("Created Apple reminders", str(result.created_apple))
            result_table.add_row(
                "Skipped existing Apple Reminder ID",
                str(result.skipped_existing_receipt),
            )
            result_table.add_row(
                "Skipped non-CREATE_APPLE actions",
                str(result.skipped_non_create_apple),
            )
            console.print(result_table)

            _, post_plan = await read_plan(notion, reminders)
            if post_plan is None:
                return False
            print_sync_plan("Post-Apply Plan", post_plan)
            console.print(
                "\n[green]Milestone 3 Notion → Apple apply completed.[/green]\n"
                "[dim]Only CREATE_APPLE actions for Notion Sync Test were eligible "
                "for writes.[/dim]"
            )
            return True

        except CreateApplePartialFailure as exc:
            console.print(f"[red]Partial success:[/red] {exc}")
            return False
        except httpx.HTTPStatusError as exc:
            response = exc.response
            console.print(
                f"[red]Notion create-Apple sync failed:[/red] "
                f"HTTP {response.status_code} {response.reason_phrase}"
            )
            return False
        except httpx.HTTPError as exc:
            console.print(f"[red]Notion create-Apple sync failed:[/red] {exc}")
            return False
        except ValueError as exc:
            console.print(f"[red]Refusing to apply:[/red] {exc}")
            return False
        except Exception as exc:
            console.print(f"[red]Notion create-Apple sync failed:[/red] {exc}")
            return False

    passed = asyncio.run(run_create_apple())
    if not passed:
        raise typer.Exit(1)


@reminders_app.command("notion-create-notion")
def reminders_notion_create_notion(
    ctx: typer.Context,
    data_source_id: str = typer.Option(
        DEFAULT_TASKS_DATA_SOURCE_ID,
        "--data-source-id",
        help="Notion Tasks data source ID to create into",
    ),
    apple_calendar: str = typer.Option(
        "Notion Sync Test",
        "--apple-calendar",
        "-a",
        help="Apple Reminders list to read from",
    ),
    notion_token: Optional[str] = typer.Option(
        None,
        "--notion-token",
        envvar="NOTION_API_TOKEN",
        help="Notion API token. Defaults to NOTION_API_TOKEN or ntn file auth.",
    ),
    api_version: str = typer.Option(
        DEFAULT_NOTION_API_VERSION,
        "--notion-version",
        help="Notion API version header",
    ),
    notion_page_size: int = typer.Option(
        100,
        "--notion-page-size",
        min=1,
        max=100,
        help="Notion page size for the enrolled-row read",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually create Notion rows for CREATE_NOTION actions",
    ),
    allow_multiple_test_creates: bool = typer.Option(
        False,
        "--allow-multiple-test-creates",
        help="Allow more than one CREATE_NOTION action from the Notion Sync Test list",
    ),
) -> None:
    """Create Notion rows from Apple test-slice reminders, gated by --apply."""

    def date_label(value) -> str:
        if value is None:
            return ""
        return value.isoformat()

    def print_sync_plan(title: str, sync_plan) -> None:
        summary = Table(title=title)
        summary.add_column("Action", style="cyan")
        summary.add_column("Count", style="green", justify="right")
        for kind in ("NOOP", "CREATE_APPLE", "CREATE_NOTION"):
            summary.add_row(kind, str(sync_plan.counts[kind]))
        console.print(summary)

        actions_table = Table(title="Planned Actions")
        actions_table.add_column("Action", style="cyan")
        actions_table.add_column("Direction")
        actions_table.add_column("Title")
        actions_table.add_column("Status/Completed")
        actions_table.add_column("Due")
        actions_table.add_column("Source ID")
        for action in sync_plan.actions:
            actions_table.add_row(
                action.kind,
                action.direction,
                action.title,
                action.status,
                date_label(action.due_date),
                action.source_id,
            )
        console.print(actions_table)

    async def read_plan(notion, reminders):
        report = await notion.preflight_schema(data_source_id)
        if not report.ok:
            _print_notion_schema_report(report)
            return None

        notion_tasks = await notion.query_tasks_with_apple_sync_id(
            data_source_id=data_source_id,
            page_size=notion_page_size,
        )

        if not await reminders.request_access():
            console.print("[red]Apple Reminders access was not granted.[/red]")
            return None

        calendars = await reminders.list_calendars()
        target_calendar = next((cal for cal in calendars if cal.title == apple_calendar), None)
        if not target_calendar:
            console.print(f"[red]Apple Reminders list not found:[/red] {apple_calendar!r}")
            return None

        apple_reminders = await reminders.get_reminders(calendar_id=target_calendar.uuid)
        match_report = build_readonly_match_report(notion_tasks, apple_reminders)
        return build_readonly_sync_plan(match_report)

    async def run_create_notion() -> bool:
        from icloudbridge.sources.reminders.eventkit import RemindersAdapter
        from icloudbridge.utils.db import NotionRemindersDB

        if apple_calendar != "Notion Sync Test":
            console.print(
                "[red]Refusing to run:[/red] Milestone 4 writes are limited to "
                "'Notion Sync Test'."
            )
            return False

        try:
            token = load_notion_token(notion_token)
        except NotionAuthError as exc:
            console.print(f"[red]Notion auth failed:[/red] {exc}")
            return False

        notion = NotionTasksAdapter(token=token, api_version=api_version)
        reminders = RemindersAdapter()

        try:
            sync_plan = await read_plan(notion, reminders)
            if sync_plan is None:
                return False
            print_sync_plan("Milestone 4 Apple → Notion Plan", sync_plan)

            if not apply:
                console.print(
                    "\n[yellow]Dry run only.[/yellow] Pass --apply to create Notion rows.\n"
                    "[dim]No Notion pages or Apple reminders were created, updated, "
                    "completed, cancelled, or deleted.[/dim]"
                )
                return True

            db = NotionRemindersDB(ctx.obj["config"].reminders_db_path)
            await db.initialize()
            result = await execute_create_notion_plan(
                sync_plan,
                data_source_id=data_source_id,
                apple_calendar_name=apple_calendar,
                notion_adapter=notion,
                db=db,
                allow_multiple_test_creates=allow_multiple_test_creates,
            )

            result_table = Table(title="Milestone 4 Apply Result")
            result_table.add_column("Metric", style="cyan")
            result_table.add_column("Count", style="green", justify="right")
            result_table.add_row("Created Notion rows", str(result.created_notion))
            result_table.add_row(
                "Skipped non-CREATE_NOTION actions",
                str(result.skipped_non_create_notion),
            )
            console.print(result_table)

            post_plan = await read_plan(notion, reminders)
            if post_plan is None:
                return False
            print_sync_plan("Post-Apply Plan", post_plan)
            console.print(
                "\n[green]Milestone 4 Apple → Notion apply completed.[/green]\n"
                "[dim]Only CREATE_NOTION actions from Notion Sync Test were eligible "
                "for writes.[/dim]"
            )
            return True

        except CreateNotionPartialFailure as exc:
            console.print(f"[red]Partial success:[/red] {exc}")
            return False
        except httpx.HTTPStatusError as exc:
            response = exc.response
            console.print(
                f"[red]Apple-to-Notion sync failed:[/red] "
                f"HTTP {response.status_code} {response.reason_phrase}"
            )
            return False
        except httpx.HTTPError as exc:
            console.print(f"[red]Apple-to-Notion sync failed:[/red] {exc}")
            return False
        except ValueError as exc:
            console.print(f"[red]Refusing to apply:[/red] {exc}")
            return False
        except Exception as exc:
            console.print(f"[red]Apple-to-Notion sync failed:[/red] {exc}")
            return False

    passed = asyncio.run(run_create_notion())
    if not passed:
        raise typer.Exit(1)


def _date_label(value) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _print_update_plan(title: str, update_plan) -> None:
    summary = Table(title=title)
    summary.add_column("Action", style="cyan")
    summary.add_column("Count", style="green", justify="right")
    for kind in ("NOOP", "NEEDS_BASELINE", "UPDATE_APPLE", "UPDATE_NOTION", "CONFLICT"):
        summary.add_row(kind, str(update_plan.counts[kind]))
    console.print(summary)

    actions_table = Table(title="Planned Update Actions")
    actions_table.add_column("Action", style="cyan")
    actions_table.add_column("Direction")
    actions_table.add_column("Title")
    actions_table.add_column("Status")
    actions_table.add_column("Due")
    actions_table.add_column("Changed Fields")
    actions_table.add_column("Source ID")
    for action in update_plan.actions:
        actions_table.add_row(
            action.kind,
            action.direction,
            action.title,
            action.status,
            _date_label(action.due_date),
            ", ".join(action.changed_fields),
            action.source_id,
        )
    console.print(actions_table)


async def _read_notion_update_plan(
    data_source_id: str,
    apple_calendar: str,
    notion_token: str | None,
    api_version: str,
    notion_page_size: int,
    db_path: Path,
):
    from icloudbridge.sources.reminders.eventkit import RemindersAdapter
    from icloudbridge.utils.db import NotionRemindersDB

    token = load_notion_token(notion_token)
    notion = NotionTasksAdapter(token=token, api_version=api_version)
    report = await notion.preflight_schema(data_source_id)
    if not report.ok:
        _print_notion_schema_report(report)
        return None, None, None, None

    notion_tasks = await notion.query_tasks_with_apple_sync_id(
        data_source_id=data_source_id,
        page_size=notion_page_size,
    )
    reminders = RemindersAdapter()
    if not await reminders.request_access():
        console.print("[red]Apple Reminders access was not granted.[/red]")
        return None, None, None, None

    calendars = await reminders.list_calendars()
    target_calendar = next((cal for cal in calendars if cal.title == apple_calendar), None)
    if not target_calendar:
        console.print(f"[red]Apple Reminders list not found:[/red] {apple_calendar!r}")
        return None, None, None, None

    apple_reminders = await reminders.get_reminders(calendar_id=target_calendar.uuid)
    match_report = build_readonly_match_report(notion_tasks, apple_reminders)
    db = NotionRemindersDB(db_path)
    await db.initialize()
    mappings = await db.get_all_notion_reminder_mappings()
    update_plan = build_bidirectional_update_plan(match_report, mappings)
    return update_plan, notion, reminders, db


@reminders_app.command("notion-update-plan")
def reminders_notion_update_plan(
    ctx: typer.Context,
    data_source_id: str = typer.Option(
        DEFAULT_TASKS_DATA_SOURCE_ID,
        "--data-source-id",
        help="Notion Tasks data source ID to read",
    ),
    apple_calendar: str = typer.Option(
        "Notion Sync Test",
        "--apple-calendar",
        "-a",
        help="Apple Reminders list to compare",
    ),
    notion_token: Optional[str] = typer.Option(
        None,
        "--notion-token",
        envvar="NOTION_API_TOKEN",
        help="Notion API token. Defaults to NOTION_API_TOKEN or ntn file auth.",
    ),
    api_version: str = typer.Option(
        DEFAULT_NOTION_API_VERSION,
        "--notion-version",
        help="Notion API version header",
    ),
    notion_page_size: int = typer.Option(
        100,
        "--notion-page-size",
        min=1,
        max=100,
        help="Notion page size for the enrolled-row read",
    ),
    write_baseline: bool = typer.Option(
        False,
        "--write-baseline",
        help="Write snapshot baselines for matched rows only",
    ),
) -> None:
    """Print Milestone 5 matched-row update actions, optionally writing baselines."""

    async def run_update_plan() -> bool:
        if write_baseline and apple_calendar != "Notion Sync Test":
            console.print(
                "[red]Refusing to write baseline:[/red] Milestone 5 baselines are "
                "limited to 'Notion Sync Test'."
            )
            return False
        try:
            update_plan, _, _, db = await _read_notion_update_plan(
                data_source_id,
                apple_calendar,
                notion_token,
                api_version,
                notion_page_size,
                ctx.obj["config"].reminders_db_path,
            )
        except NotionAuthError as exc:
            console.print(f"[red]Notion auth failed:[/red] {exc}")
            return False
        except httpx.HTTPStatusError as exc:
            response = exc.response
            console.print(
                f"[red]Notion update planning read failed:[/red] "
                f"HTTP {response.status_code} {response.reason_phrase}"
            )
            return False
        except Exception as exc:
            console.print(f"[red]Update planning failed:[/red] {exc}")
            return False

        if update_plan is None or db is None:
            return False
        if apple_calendar != "Notion Sync Test":
            console.print(
                f"[yellow]Warning:[/yellow] planning against non-test list "
                f"{apple_calendar!r}; this command is read-only."
            )
        _print_update_plan("Milestone 5A Update Plan", update_plan)

        if write_baseline:
            count = 0
            for action in update_plan.actions:
                if action.notion_task is None or action.apple_reminder is None:
                    continue
                await db.update_notion_reminder_snapshots(
                    apple_sync_id=action.notion_task.apple_sync_id
                    or (action.mapping or {}).get("apple_sync_id")
                    or action.notion_task.page_id,
                    notion_snapshot=build_notion_snapshot(action.notion_task),
                    apple_snapshot=build_apple_snapshot(action.apple_reminder),
                    timestamp=datetime.now(timezone.utc),
                )
                count += 1
            console.print(f"\n[green]Wrote {count} matched-row snapshot baselines.[/green]")
        else:
            console.print(
                "\n[green]Milestone 5A update plan completed.[/green]\n"
                "[dim]No Notion fields or Apple reminders were updated.[/dim]"
            )
        return True

    passed = asyncio.run(run_update_plan())
    if not passed:
        raise typer.Exit(1)


@reminders_app.command("notion-update-apple")
def reminders_notion_update_apple(
    ctx: typer.Context,
    data_source_id: str = typer.Option(DEFAULT_TASKS_DATA_SOURCE_ID, "--data-source-id"),
    apple_calendar: str = typer.Option("Notion Sync Test", "--apple-calendar", "-a"),
    notion_token: Optional[str] = typer.Option(
        None,
        "--notion-token",
        envvar="NOTION_API_TOKEN",
    ),
    api_version: str = typer.Option(DEFAULT_NOTION_API_VERSION, "--notion-version"),
    notion_page_size: int = typer.Option(100, "--notion-page-size", min=1, max=100),
    apply: bool = typer.Option(False, "--apply", help="Actually update Apple reminders"),
    allow_multiple_test_updates: bool = typer.Option(
        False,
        "--allow-multiple-test-updates",
        help="Allow more than one UPDATE_APPLE action in the test list",
    ),
) -> None:
    """Apply Notion-to-Apple update actions, gated by --apply."""

    async def run_update_apple() -> bool:
        if apple_calendar != "Notion Sync Test":
            console.print(
                "[red]Refusing to run:[/red] Milestone 5 Apple updates are limited to "
                "'Notion Sync Test'."
            )
            return False
        try:
            update_plan, _, reminders, db = await _read_notion_update_plan(
                data_source_id,
                apple_calendar,
                notion_token,
                api_version,
                notion_page_size,
                ctx.obj["config"].reminders_db_path,
            )
            if update_plan is None or reminders is None or db is None:
                return False
            _print_update_plan("Milestone 5B Notion → Apple Update Plan", update_plan)
            if not apply:
                console.print(
                    "\n[yellow]Dry run only.[/yellow] Pass --apply to update Apple reminders."
                )
                return True
            result = await execute_update_apple_plan(
                update_plan,
                apple_calendar_name=apple_calendar,
                reminders_adapter=reminders,
                db=db,
                allow_multiple_test_updates=allow_multiple_test_updates,
            )
            result_table = Table(title="Milestone 5B Apply Result")
            result_table.add_column("Metric", style="cyan")
            result_table.add_column("Count", style="green", justify="right")
            result_table.add_row("Updated Apple reminders", str(result.updated_apple))
            result_table.add_row(
                "Skipped non-UPDATE_APPLE actions",
                str(result.skipped_non_update_apple),
            )
            console.print(result_table)
            post_plan, _, _, _ = await _read_notion_update_plan(
                data_source_id,
                apple_calendar,
                notion_token,
                api_version,
                notion_page_size,
                ctx.obj["config"].reminders_db_path,
            )
            if post_plan is None:
                return False
            _print_update_plan("Post-Apply Update Plan", post_plan)
            return True
        except Exception as exc:
            console.print(f"[red]Notion-to-Apple update failed:[/red] {exc}")
            return False

    passed = asyncio.run(run_update_apple())
    if not passed:
        raise typer.Exit(1)


@reminders_app.command("notion-update-notion")
def reminders_notion_update_notion(
    ctx: typer.Context,
    data_source_id: str = typer.Option(DEFAULT_TASKS_DATA_SOURCE_ID, "--data-source-id"),
    apple_calendar: str = typer.Option("Notion Sync Test", "--apple-calendar", "-a"),
    notion_token: Optional[str] = typer.Option(
        None,
        "--notion-token",
        envvar="NOTION_API_TOKEN",
    ),
    api_version: str = typer.Option(DEFAULT_NOTION_API_VERSION, "--notion-version"),
    notion_page_size: int = typer.Option(100, "--notion-page-size", min=1, max=100),
    apply: bool = typer.Option(False, "--apply", help="Actually update Notion rows"),
    allow_multiple_test_updates: bool = typer.Option(
        False,
        "--allow-multiple-test-updates",
        help="Allow more than one UPDATE_NOTION action in the test list",
    ),
) -> None:
    """Apply Apple-to-Notion update actions, gated by --apply."""

    async def run_update_notion() -> bool:
        if apple_calendar != "Notion Sync Test":
            console.print(
                "[red]Refusing to run:[/red] Milestone 5 Notion updates are limited to "
                "'Notion Sync Test'."
            )
            return False
        try:
            update_plan, notion, _, db = await _read_notion_update_plan(
                data_source_id,
                apple_calendar,
                notion_token,
                api_version,
                notion_page_size,
                ctx.obj["config"].reminders_db_path,
            )
            if update_plan is None or notion is None or db is None:
                return False
            _print_update_plan("Milestone 5C Apple → Notion Update Plan", update_plan)
            if not apply:
                console.print(
                    "\n[yellow]Dry run only.[/yellow] Pass --apply to update Notion rows."
                )
                return True
            result = await execute_update_notion_plan(
                update_plan,
                apple_calendar_name=apple_calendar,
                notion_adapter=notion,
                db=db,
                allow_multiple_test_updates=allow_multiple_test_updates,
            )
            result_table = Table(title="Milestone 5C Apply Result")
            result_table.add_column("Metric", style="cyan")
            result_table.add_column("Count", style="green", justify="right")
            result_table.add_row("Updated Notion rows", str(result.updated_notion))
            result_table.add_row(
                "Skipped non-UPDATE_NOTION actions",
                str(result.skipped_non_update_notion),
            )
            console.print(result_table)
            post_plan, _, _, _ = await _read_notion_update_plan(
                data_source_id,
                apple_calendar,
                notion_token,
                api_version,
                notion_page_size,
                ctx.obj["config"].reminders_db_path,
            )
            if post_plan is None:
                return False
            _print_update_plan("Post-Apply Update Plan", post_plan)
            return True
        except Exception as exc:
            console.print(f"[red]Apple-to-Notion update failed:[/red] {exc}")
            return False

    passed = asyncio.run(run_update_notion())
    if not passed:
        raise typer.Exit(1)


def _pick_proof_noop_action(update_plan, direction: str):
    candidates = [
        action
        for action in update_plan.actions
        if action.kind == "NOOP"
        and action.notion_task is not None
        and action.apple_reminder is not None
    ]
    preferred = "Notion to Apple" if direction == "notion-to-apple" else "Apple to Notion"
    for action in candidates:
        if preferred in action.title or preferred in getattr(action.apple_reminder, "title", ""):
            return action
    if candidates:
        return candidates[0]
    raise ValueError("Proof requires one matched NOOP row to mutate")


async def _baseline_update_actions(db, update_plan) -> int:
    count = 0
    for action in update_plan.actions:
        if action.notion_task is None or action.apple_reminder is None:
            continue
        await db.update_notion_reminder_snapshots(
            apple_sync_id=action.notion_task.apple_sync_id
            or (action.mapping or {}).get("apple_sync_id")
            or action.notion_task.page_id,
            notion_snapshot=build_notion_snapshot(action.notion_task),
            apple_snapshot=build_apple_snapshot(action.apple_reminder),
            timestamp=datetime.now(timezone.utc),
        )
        count += 1
    return count


@reminders_app.command("notion-update-proof")
def reminders_notion_update_proof(
    ctx: typer.Context,
    data_source_id: str = typer.Option(DEFAULT_TASKS_DATA_SOURCE_ID, "--data-source-id"),
    apple_calendar: str = typer.Option("Notion Sync Test", "--apple-calendar", "-a"),
    field: str = typer.Option(..., "--field", help="Field to prove"),
    direction: str = typer.Option(..., "--direction", help="Proof direction"),
    notion_token: Optional[str] = typer.Option(
        None,
        "--notion-token",
        envvar="NOTION_API_TOKEN",
    ),
    api_version: str = typer.Option(DEFAULT_NOTION_API_VERSION, "--notion-version"),
    notion_page_size: int = typer.Option(100, "--notion-page-size", min=1, max=100),
    apply: bool = typer.Option(False, "--apply", help="Actually run the live proof"),
) -> None:
    """Run one Milestone 5D test-slice field proof."""

    async def run_update_proof() -> bool:
        if apple_calendar != "Notion Sync Test":
            console.print("[red]Refusing proof:[/red] only 'Notion Sync Test' is allowed.")
            return False
        if not apply:
            console.print("[red]Refusing proof:[/red] --apply is required for live mutations.")
            return False

        try:
            mutation = build_proof_mutation(field, direction)
            initial_plan, notion, reminders, db = await _read_notion_update_plan(
                data_source_id,
                apple_calendar,
                notion_token,
                api_version,
                notion_page_size,
                ctx.obj["config"].reminders_db_path,
            )
            if initial_plan is None or notion is None or reminders is None or db is None:
                return False
            assert_proof_ready_plan(initial_plan)
            target = _pick_proof_noop_action(initial_plan, direction)
            assert_expected_proof_plan(
                build_bidirectional_update_plan(
                    build_readonly_match_report(
                        [target.notion_task],
                        [target.apple_reminder],
                    ),
                    [target.mapping] if target.mapping else [],
                ),
                "NOOP",
            )

            original_title = target.title
            original_page_id = target.notion_task.page_id
            original_reminder_id = getattr(target.apple_reminder, "uuid", "")
            if not original_title.startswith("[SYNC TEST]"):
                raise ValueError("Proof may only mutate [SYNC TEST] rows")

            if direction == "notion-to-apple":
                updates = dict(mutation.notion_updates)
                if field == "completion":
                    updates["completed"] = not target.notion_task.completed
                await notion.update_task_proof_fields(
                    page_id=target.notion_task.page_id,
                    clear_due_date=field == "clear-due",
                    **updates,
                )
                expected_action = "UPDATE_APPLE"
            else:
                updates = dict(mutation.apple_updates)
                if field == "completion":
                    updates["completed"] = not getattr(target.apple_reminder, "completed", False)
                await reminders.update_reminder(
                    uuid=getattr(target.apple_reminder, "uuid", ""),
                    **updates,
                )
                expected_action = "UPDATE_NOTION"

            proof_plan, _, _, _ = await _read_notion_update_plan(
                data_source_id,
                apple_calendar,
                notion_token,
                api_version,
                notion_page_size,
                ctx.obj["config"].reminders_db_path,
            )
            if proof_plan is None:
                return False
            assert_expected_proof_plan(proof_plan, expected_action)

            if direction == "notion-to-apple":
                first = await execute_update_apple_plan(
                    proof_plan,
                    apple_calendar_name=apple_calendar,
                    reminders_adapter=reminders,
                    db=db,
                )
                first_count = first.updated_apple
                second_plan, _, _, _ = await _read_notion_update_plan(
                    data_source_id,
                    apple_calendar,
                    notion_token,
                    api_version,
                    notion_page_size,
                    ctx.obj["config"].reminders_db_path,
                )
                if second_plan is None:
                    return False
                assert_proof_ready_plan(second_plan)
                second = await execute_update_apple_plan(
                    second_plan,
                    apple_calendar_name=apple_calendar,
                    reminders_adapter=reminders,
                    db=db,
                )
                second_count = second.updated_apple
            else:
                first = await execute_update_notion_plan(
                    proof_plan,
                    apple_calendar_name=apple_calendar,
                    notion_adapter=notion,
                    db=db,
                )
                first_count = first.updated_notion
                second_plan, _, _, _ = await _read_notion_update_plan(
                    data_source_id,
                    apple_calendar,
                    notion_token,
                    api_version,
                    notion_page_size,
                    ctx.obj["config"].reminders_db_path,
                )
                if second_plan is None:
                    return False
                assert_proof_ready_plan(second_plan)
                second = await execute_update_notion_plan(
                    second_plan,
                    apple_calendar_name=apple_calendar,
                    notion_adapter=notion,
                    db=db,
                )
                second_count = second.updated_notion

            if first_count != 1:
                raise ValueError(f"Proof expected first apply to update 1 row, got {first_count}")
            if second_count != 0:
                raise ValueError(f"Proof expected second apply to update 0 rows, got {second_count}")

            final_plan, _, _, _ = await _read_notion_update_plan(
                data_source_id,
                apple_calendar,
                notion_token,
                api_version,
                notion_page_size,
                ctx.obj["config"].reminders_db_path,
            )
            if final_plan is None:
                return False
            assert_proof_ready_plan(final_plan)

            if mutation.restore_title:
                await notion.update_task_proof_fields(
                    page_id=original_page_id,
                    title=original_title,
                )
                await reminders.update_reminder(
                    uuid=original_reminder_id,
                    title=original_title,
                )
                restored_plan, _, _, restored_db = await _read_notion_update_plan(
                    data_source_id,
                    apple_calendar,
                    notion_token,
                    api_version,
                    notion_page_size,
                    ctx.obj["config"].reminders_db_path,
                )
                if restored_plan is None or restored_db is None:
                    return False
                await _baseline_update_actions(restored_db, restored_plan)
                final_plan, _, _, _ = await _read_notion_update_plan(
                    data_source_id,
                    apple_calendar,
                    notion_token,
                    api_version,
                    notion_page_size,
                    ctx.obj["config"].reminders_db_path,
                )
                if final_plan is None:
                    return False
                assert_proof_ready_plan(final_plan)

            receipt = Table(title="Milestone 5D Proof Result")
            receipt.add_column("Metric", style="cyan")
            receipt.add_column("Value", style="green")
            receipt.add_row("Field", field)
            receipt.add_row("Direction", direction)
            receipt.add_row("Source mutation", mutation.summary)
            receipt.add_row("Expected action", expected_action)
            receipt.add_row("First apply updates", str(first_count))
            receipt.add_row("Second apply updates", str(second_count))
            receipt.add_row("Final counts", str(final_plan.counts))
            console.print(receipt)
            return True

        except Exception as exc:
            console.print(f"[red]Milestone 5D proof failed:[/red] {exc}")
            return False

    passed = asyncio.run(run_update_proof())
    if not passed:
        raise typer.Exit(1)


@reminders_app.command("sync")
def reminders_sync(
    ctx: typer.Context,
    apple_calendar: Optional[str] = typer.Option(
        None,
        "--apple-calendar",
        "-a",
        help="Apple Reminders calendar/list to sync (manual mode)",
    ),
    caldav_calendar: Optional[str] = typer.Option(
        None,
        "--caldav-calendar",
        "-c",
        help="CalDAV calendar to sync with (manual mode)",
    ),
    auto: bool = typer.Option(
        None,
        "--auto/--no-auto",
        help="Auto-discover and sync all calendars (default: from config)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-n",
        help="Preview changes without applying them",
    ),
    skip_deletions: bool = typer.Option(
        False,
        "--skip-deletions",
        help="Skip all deletion operations",
    ),
    deletion_threshold: int = typer.Option(
        5,
        "--deletion-threshold",
        help="Max deletions before confirmation (use -1 to disable)",
    ),
    verify_ssl: Optional[bool] = typer.Option(
        None,
        "--verify-ssl/--no-verify-ssl",
        help="Override CalDAV SSL certificate verification (default: use config value)",
    ),
) -> None:
    """
    Synchronize reminders between Apple Reminders and CalDAV.

    By default (auto mode), syncs all calendars:
    - "Reminders" → "tasks" (NextCloud default)
    - Other Apple calendars → CalDAV calendars with matching names

    Use --apple-calendar and --caldav-calendar for manual single-calendar sync.
    """
    cfg = ctx.obj["config"]

    # Check if reminders sync is enabled
    if not cfg.reminders.enabled:
        console.print("[red]Reminders sync is not enabled in configuration[/red]")
        console.print("[dim]Enable it in your config file or use environment variables[/dim]")
        raise typer.Exit(1)

    # Check if CalDAV is configured
    if not cfg.reminders.caldav_url:
        console.print("[red]CalDAV URL is not configured[/red]")
        console.print("[dim]Set ICLOUDBRIDGE_REMINDERS__CALDAV_URL in your config[/dim]")
        raise typer.Exit(1)

    # Get password from keyring or config
    caldav_password = cfg.reminders.get_caldav_password()

    if not cfg.reminders.caldav_username or not caldav_password:
        console.print("[red]CalDAV credentials not configured[/red]")
        console.print("[dim]Set username with ICLOUDBRIDGE_REMINDERS__CALDAV_USERNAME[/dim]")
        console.print("[dim]Set password with: icloudbridge reminders set-password[/dim]")
        raise typer.Exit(1)

    ssl_verify_cert = cfg.reminders.caldav_ssl_verify_cert if verify_ssl is None else verify_ssl
    if ssl_verify_cert is False:
        console.print("[yellow]Certificate verification is disabled for this run.[/yellow]")
        console.print("[dim]Use only with trusted self-signed certificates.[/dim]")

    # Determine sync mode
    use_auto = auto if auto is not None else (cfg.reminders.sync_mode == "auto")

    # Manual mode: specific calendar pair
    if apple_calendar and caldav_calendar:
        use_auto = False

    if dry_run:
        console.print("[cyan]DRY RUN MODE: Previewing changes only[/cyan]\n")

    async def run_sync():
        # Initialize sync engine
        sync_engine = RemindersSyncEngine(
            caldav_url=cfg.reminders.caldav_url,
            caldav_username=cfg.reminders.caldav_username,
            caldav_password=caldav_password,
            db_path=cfg.reminders_db_path,
            caldav_ssl_verify_cert=ssl_verify_cert,
        )
        await sync_engine.initialize()

        try:
            if use_auto:
                # Auto mode: discover and sync all calendars
                console.print("[bold cyan]Auto Mode:[/bold cyan] Discovering and syncing all calendars\n")

                all_stats = await sync_engine.discover_and_sync_all(
                    base_mappings=cfg.reminders.calendar_mappings,
                    dry_run=dry_run,
                    skip_deletions=skip_deletions,
                    deletion_threshold=deletion_threshold,
                )

                # Show summary table
                table = Table(title="Sync Results")
                table.add_column("Calendar Pair", style="cyan")
                table.add_column("Created", style="green", justify="right")
                table.add_column("Updated", style="yellow", justify="right")
                table.add_column("Deleted", style="red", justify="right")
                table.add_column("Unchanged", style="dim", justify="right")
                table.add_column("Errors", style="red bold", justify="right")

                total_stats = {
                    "created": 0,
                    "updated": 0,
                    "deleted": 0,
                    "unchanged": 0,
                    "errors": 0,
                }

                for pair_name, stats in all_stats.items():
                    created = stats['created_local'] + stats['created_remote']
                    updated = stats['updated_local'] + stats['updated_remote']
                    deleted = stats['deleted_local'] + stats['deleted_remote']

                    total_stats["created"] += created
                    total_stats["updated"] += updated
                    total_stats["deleted"] += deleted
                    total_stats["unchanged"] += stats['unchanged']
                    total_stats["errors"] += stats['errors']

                    table.add_row(
                        pair_name,
                        str(created),
                        str(updated),
                        str(deleted),
                        str(stats['unchanged']),
                        str(stats['errors']) if stats['errors'] > 0 else "-",
                    )

                console.print(table)
                console.print(f"\n[bold green]Total:[/bold green] {total_stats['created']} created, "
                             f"{total_stats['updated']} updated, {total_stats['deleted']} deleted, "
                             f"{total_stats['unchanged']} unchanged")
                if total_stats['errors'] > 0:
                    console.print(f"[red]Errors: {total_stats['errors']}[/red]")

            else:
                # Manual mode: single calendar pair
                # Use legacy config or CLI args
                effective_apple_cal = apple_calendar or cfg.reminders.apple_calendar
                effective_caldav_cal = caldav_calendar or cfg.reminders.caldav_calendar

                if not effective_apple_cal or not effective_caldav_cal:
                    console.print("[red]Manual mode requires --apple-calendar and --caldav-calendar[/red]")
                    console.print("[dim]Or use auto mode with --auto (or set sync_mode=auto in config)[/dim]")
                    raise typer.Exit(1)

                console.print(f"[cyan]Manual Mode:[/cyan] Syncing {effective_apple_cal} → {effective_caldav_cal}\n")

                stats = await sync_engine.sync_calendar(
                    apple_calendar_name=effective_apple_cal,
                    caldav_calendar_name=effective_caldav_cal,
                    dry_run=dry_run,
                    skip_deletions=skip_deletions,
                    deletion_threshold=deletion_threshold,
                )

                # Show stats
                console.print(f"\n[bold green]Sync completed![/bold green]")
                console.print(f"  Created in Apple Reminders: {stats['created_local']}")
                console.print(f"  Created in CalDAV: {stats['created_remote']}")
                console.print(f"  Updated in Apple Reminders: {stats['updated_local']}")
                console.print(f"  Updated in CalDAV: {stats['updated_remote']}")
                console.print(f"  Deleted from Apple Reminders: {stats['deleted_local']}")
                console.print(f"  Deleted from CalDAV: {stats['deleted_remote']}")
                console.print(f"  Unchanged: {stats['unchanged']}")
                if stats['errors'] > 0:
                    console.print(f"  [red]Errors: {stats['errors']}[/red]")

        except Exception as e:
            console.print(f"[red]Sync failed: {e}[/red]")
            raise typer.Exit(1)

    asyncio.run(run_sync())


@reminders_app.command("list")
def reminders_list(ctx: typer.Context) -> None:
    """List Apple Reminders calendars/lists."""
    cfg = ctx.obj["config"]

    if not cfg.reminders.enabled:
        console.print("[red]Reminders sync is not enabled in configuration[/red]")
        raise typer.Exit(1)

    async def list_calendars():
        from icloudbridge.sources.reminders.eventkit import RemindersAdapter

        adapter = RemindersAdapter()
        await adapter.request_access()

        calendars = await adapter.list_calendars()

        if not calendars:
            console.print("[yellow]No reminder calendars found[/yellow]")
            return

        table = Table(title="Apple Reminders Calendars")
        table.add_column("Name", style="cyan", no_wrap=True)
        table.add_column("UUID", style="dim")

        for cal in calendars:
            table.add_row(cal.title, cal.uuid)

        console.print(table)

    asyncio.run(list_calendars())


@reminders_app.command("status")
def reminders_status(ctx: typer.Context) -> None:
    """Show reminders sync status."""
    cfg = ctx.obj["config"]

    # Check configuration
    console.print("[bold]Reminders Sync Status[/bold]\n")

    if cfg.reminders.enabled:
        console.print("✓ Reminders sync enabled", style="green")
    else:
        console.print("✗ Reminders sync disabled", style="red")
        return

    if cfg.reminders.caldav_url:
        console.print(f"✓ CalDAV URL: {cfg.reminders.caldav_url}", style="green")
    else:
        console.print("✗ CalDAV URL not configured", style="red")

    if cfg.reminders.caldav_username:
        console.print(f"✓ CalDAV username: {cfg.reminders.caldav_username}", style="green")

        # Check password source
        password = cfg.reminders.get_caldav_password()
        if password:
            from icloudbridge.utils.credentials import CredentialStore

            cred_store = CredentialStore()
            if cred_store.has_caldav_password(cfg.reminders.caldav_username):
                console.print("✓ CalDAV password: stored in system keyring (secure)", style="green")
            else:
                console.print(
                    "✓ CalDAV password: configured in config/env (consider using keyring)",
                    style="yellow",
                )
        else:
            console.print("✗ CalDAV password not configured", style="red")
    else:
        console.print("✗ CalDAV username not configured", style="red")

    if cfg.reminders.apple_calendar:
        console.print(f"✓ Apple calendar: {cfg.reminders.apple_calendar}", style="green")
    else:
        console.print("ℹ Apple calendar not configured (can specify with --apple-calendar)", style="yellow")

    if cfg.reminders.caldav_calendar:
        console.print(f"✓ CalDAV calendar: {cfg.reminders.caldav_calendar}", style="green")
    else:
        console.print("ℹ CalDAV calendar not configured (can specify with --caldav-calendar)", style="yellow")

    console.print("\n[dim]Status: Ready[/dim]")


@reminders_app.command("reset")
def reminders_reset(
    ctx: typer.Context,
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt",
    ),
) -> None:
    """Reset reminders sync database (clear all mappings)."""
    cfg = ctx.obj["config"]

    if not yes:
        console.print("[yellow]This will clear all reminder sync mappings from the database.[/yellow]")
        console.print("[dim]Your reminders will NOT be deleted, only the sync tracking.[/dim]\n")
        confirmed = typer.confirm("Are you sure you want to continue?")
        if not confirmed:
            console.print("[dim]Cancelled[/dim]")
            raise typer.Exit(0)

    async def reset_db():
        sync_engine = RemindersSyncEngine(
            caldav_url=cfg.reminders.caldav_url or "http://dummy.url",
            caldav_username=cfg.reminders.caldav_username or "dummy",
            caldav_password=cfg.reminders.caldav_password or "dummy",
            db_path=cfg.reminders_db_path,
            caldav_ssl_verify_cert=cfg.reminders.caldav_ssl_verify_cert,
        )
        await sync_engine.db.initialize()
        await sync_engine.reset_database()
        console.print("[green]✓ Database reset complete[/green]")

    asyncio.run(reset_db())


@reminders_app.command("set-password")
def reminders_set_password(
    ctx: typer.Context,
    username: Optional[str] = typer.Option(
        None,
        "--username",
        "-u",
        help="CalDAV username (default: from config)",
    ),
) -> None:
    """Store CalDAV password securely in system keyring."""
    from icloudbridge.utils.credentials import CredentialStore

    cfg = ctx.obj["config"]

    # Use config username if not specified
    if not username:
        username = cfg.reminders.caldav_username
        if not username:
            console.print("[red]Username not specified and not found in config[/red]")
            console.print("[dim]Use --username or set ICLOUDBRIDGE_REMINDERS__CALDAV_USERNAME[/dim]")
            raise typer.Exit(1)

    # Prompt for password (hidden input)
    password = typer.prompt(f"Enter CalDAV password for {username}", hide_input=True)
    password_confirm = typer.prompt("Confirm password", hide_input=True)

    if password != password_confirm:
        console.print("[red]Passwords do not match[/red]")
        raise typer.Exit(1)

    # Store in keyring
    try:
        cred_store = CredentialStore()
        cred_store.set_caldav_password(username, password)
        console.print(f"[green]✓ Password stored securely for user: {username}[/green]")
        console.print("[dim]You can now remove CALDAV_PASSWORD from your config/environment[/dim]")
    except Exception as e:
        console.print(f"[red]Failed to store password: {e}[/red]")
        raise typer.Exit(1)


@reminders_app.command("delete-password")
def reminders_delete_password(
    ctx: typer.Context,
    username: Optional[str] = typer.Option(
        None,
        "--username",
        "-u",
        help="CalDAV username (default: from config)",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt",
    ),
) -> None:
    """Delete CalDAV password from system keyring."""
    from icloudbridge.utils.credentials import CredentialStore

    cfg = ctx.obj["config"]

    # Use config username if not specified
    if not username:
        username = cfg.reminders.caldav_username
        if not username:
            console.print("[red]Username not specified and not found in config[/red]")
            console.print("[dim]Use --username or set ICLOUDBRIDGE_REMINDERS__CALDAV_USERNAME[/dim]")
            raise typer.Exit(1)

    if not yes:
        confirmed = typer.confirm(f"Delete stored password for {username}?")
        if not confirmed:
            console.print("[dim]Cancelled[/dim]")
            raise typer.Exit(0)

    # Delete from keyring
    try:
        cred_store = CredentialStore()
        if cred_store.delete_caldav_password(username):
            console.print(f"[green]✓ Password deleted for user: {username}[/green]")
        else:
            console.print(f"[yellow]No password found for user: {username}[/yellow]")
    except Exception as e:
        console.print(f"[red]Failed to delete password: {e}[/red]")
        raise typer.Exit(1)


# ============================================================================
# PASSWORD COMMANDS
# ============================================================================

passwords_app = typer.Typer(help="Manage passwords synchronization")


@passwords_app.command(name="provider")
def passwords_provider(
    ctx: typer.Context,
    service: Optional[str] = typer.Argument(
        None,
        metavar="SERVICE",
        help="Choose 'bitwarden' or 'nextcloud'",
    ),
) -> None:
    """Select which password service to sync with and persist the choice."""
    cfg = ctx.obj["config"]

    current = cfg.passwords.provider
    if service is None:
        friendly = "Bitwarden / Vaultwarden" if current == "vaultwarden" else "Nextcloud Passwords"
        console.print(f"[green]Current provider:[/green] {friendly} [dim]({current})[/dim]")
        console.print("[dim]Set with: icloudbridge passwords provider <bitwarden|nextcloud>[/dim]")
        return

    normalized = service.strip().lower()
    if normalized in {"bitwarden", "vaultwarden"}:
        provider_value = "vaultwarden"
        display = "Bitwarden / Vaultwarden"
    elif normalized in {"nextcloud", "nextcloud-passwords", "nextcloud_passwords"}:
        provider_value = "nextcloud"
        display = "Nextcloud Passwords"
    else:
        console.print(f"[red]Unknown provider: {service}[/red]")
        console.print("[dim]Choose either 'bitwarden' or 'nextcloud'[/dim]")
        raise typer.Exit(1)

    cfg.passwords.provider = provider_value

    config_path = cfg.general.config_file or cfg.default_config_path
    try:
        cfg.ensure_data_dir()
        cfg.save_to_file(config_path)
        console.print(f"[green]✅ Password provider set to {display}[/green]")
        console.print(f"[dim]Saved to: {config_path}[/dim]")
    except ImportError as e:
        console.print(f"[red]Failed to save configuration: {e}[/red]")
        raise typer.Exit(1)


@passwords_app.command(name="import-apple")
def passwords_import_apple(
    ctx: typer.Context,
    csv_file: Path = typer.Argument(..., help="Apple Passwords CSV export file"),
) -> None:
    """Import passwords from Apple Passwords CSV export."""
    import asyncio
    from datetime import datetime

    from ..core.passwords_sync import PasswordsSyncEngine
    from ..utils.db import PasswordsDB

    cfg = ctx.obj["config"]

    console.print(Panel.fit("🔐 Apple Passwords Import", style="bold blue"))

    # Validate file exists
    if not csv_file.exists():
        console.print(f"[red]Error: File not found: {csv_file}[/red]")
        raise typer.Exit(1)

    # Initialize database
    db_path = cfg.passwords_db_path
    db = PasswordsDB(db_path)

    async def run_import():
        await db.initialize()
        engine = PasswordsSyncEngine(db)
        return await engine.import_apple_csv(csv_file)

    try:
        stats = asyncio.run(run_import())

        # Display results
        table = Table(title="Import Results")
        table.add_column("Category", style="cyan")
        table.add_column("Count", justify="right", style="green")

        table.add_row("Total processed", str(stats["total_processed"]))
        table.add_row("New entries", str(stats["new"]))
        table.add_row("Updated entries", str(stats["updated"]))
        table.add_row("Duplicates skipped", str(stats["duplicates"]))
        table.add_row("Unchanged", str(stats["unchanged"]))
        if stats["errors"] > 0:
            table.add_row("Errors", str(stats["errors"]), style="red")

        console.print(table)

        console.print(f"\n[green]✅ Import complete[/green]")
        console.print(f"   Database: {db_path}")
        console.print(f"   Last import: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        # Security warning
        console.print(
            "\n[yellow]⚠️  SECURITY WARNING[/yellow]\n"
            "   CSV file contains plaintext passwords!\n"
            f"   Delete immediately: {csv_file}\n"
        )

        # Next step suggestion
        console.print(
            "[dim]💡 Next step: Generate Bitwarden import file[/dim]\n"
            f"   → icloudbridge passwords-export-bitwarden -o bitwarden.csv --apple-csv {csv_file}"
        )

    except Exception as e:
        console.print(f"[red]Error importing passwords: {e}[/red]")
        logging.exception("Import failed")
        raise typer.Exit(1)


@passwords_app.command(name="export-bitwarden")
def passwords_export_bitwarden(
    ctx: typer.Context,
    output: Path = typer.Option(..., "-o", "--output", help="Output CSV file"),
    apple_csv: Path = typer.Option(..., help="Original Apple Passwords CSV"),
) -> None:
    """Generate Bitwarden-formatted CSV for import."""
    import asyncio
    from datetime import datetime

    from ..core.passwords_sync import PasswordsSyncEngine
    from ..utils.db import PasswordsDB

    cfg = ctx.obj["config"]

    console.print(Panel.fit("🔐 Bitwarden CSV Export", style="bold blue"))

    # Validate input file exists
    if not apple_csv.exists():
        console.print(f"[red]Error: Apple CSV not found: {apple_csv}[/red]")
        raise typer.Exit(1)

    # Initialize database
    db_path = cfg.passwords_db_path
    db = PasswordsDB(db_path)

    async def run_export():
        await db.initialize()
        engine = PasswordsSyncEngine(db)
        return await engine.export_bitwarden_csv(output, apple_csv)

    try:
        count = asyncio.run(run_export())

        console.print(f"[green]✅ Bitwarden CSV generated[/green]")
        console.print(f"   File: {output}")
        console.print(f"   Entries: {count}")
        console.print(f"   Permissions: 0600 (owner read/write only)")

        # Security warning
        console.print(
            "\n[yellow]⚠️  SECURITY WARNING[/yellow]\n"
            "   Generated CSV contains plaintext passwords!\n"
            f"   1. Import to Bitwarden immediately\n"
            f"   2. Delete file: rm {output}\n"
        )

        # Next steps
        console.print(
            "[dim]💡 Import to Bitwarden:[/dim]\n"
            "   Settings → Import Data → Bitwarden (csv)\n"
            f"   Then delete both CSV files!"
        )

    except Exception as e:
        console.print(f"[red]Error exporting to Bitwarden: {e}[/red]")
        logging.exception("Export failed")
        raise typer.Exit(1)


@passwords_app.command(name="import-bitwarden")
def passwords_import_bitwarden(
    ctx: typer.Context,
    csv_file: Path = typer.Argument(..., help="Bitwarden CSV export file"),
) -> None:
    """Import passwords from Bitwarden CSV export."""
    import asyncio
    from datetime import datetime

    from ..core.passwords_sync import PasswordsSyncEngine
    from ..utils.db import PasswordsDB

    cfg = ctx.obj["config"]

    console.print(Panel.fit("🔐 Bitwarden Import", style="bold blue"))

    # Validate file exists
    if not csv_file.exists():
        console.print(f"[red]Error: File not found: {csv_file}[/red]")
        raise typer.Exit(1)

    # Initialize database
    db_path = cfg.passwords_db_path
    db = PasswordsDB(db_path)

    async def run_import():
        await db.initialize()
        engine = PasswordsSyncEngine(db)
        return await engine.import_bitwarden_csv(csv_file)

    try:
        stats = asyncio.run(run_import())

        # Display results
        table = Table(title="Import Results")
        table.add_column("Category", style="cyan")
        table.add_column("Count", justify="right", style="green")

        table.add_row("Total processed", str(stats["total_processed"]))
        table.add_row("New entries", str(stats["new"]))
        table.add_row("Updated entries", str(stats["updated"]))
        table.add_row("Duplicates skipped", str(stats["duplicates"]))
        table.add_row("Unchanged", str(stats["unchanged"]))
        if stats["errors"] > 0:
            table.add_row("Errors", str(stats["errors"]), style="red")

        console.print(table)

        console.print(f"\n[green]✅ Import complete[/green]")
        console.print(f"   Database: {db_path}")
        console.print(f"   Last import: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        # Security warning
        console.print(
            "\n[yellow]⚠️  SECURITY WARNING[/yellow]\n"
            "   CSV file contains plaintext passwords!\n"
            f"   Delete immediately: {csv_file}\n"
        )

        # Next step suggestion
        console.print(
            "[dim]💡 Next step: Generate Apple Passwords import file[/dim]\n"
            f"   → icloudbridge passwords-export-apple -o apple-import.csv --bitwarden-csv {csv_file}"
        )

    except Exception as e:
        console.print(f"[red]Error importing passwords: {e}[/red]")
        logging.exception("Import failed")
        raise typer.Exit(1)


@passwords_app.command(name="export-apple")
def passwords_export_apple(
    ctx: typer.Context,
    output: Path = typer.Option(..., "-o", "--output", help="Output CSV file"),
    bitwarden_csv: Path = typer.Option(..., help="Original Bitwarden CSV export"),
) -> None:
    """Generate Apple Passwords CSV for entries only in Bitwarden (not in Apple)."""
    import asyncio

    from ..core.passwords_sync import PasswordsSyncEngine
    from ..utils.db import PasswordsDB

    cfg = ctx.obj["config"]

    console.print(Panel.fit("🔐 Apple Passwords Export", style="bold blue"))

    # Validate input file exists
    if not bitwarden_csv.exists():
        console.print(f"[red]Error: Bitwarden CSV not found: {bitwarden_csv}[/red]")
        raise typer.Exit(1)

    # Initialize database
    db_path = cfg.passwords_db_path
    db = PasswordsDB(db_path)

    async def run_export():
        await db.initialize()
        engine = PasswordsSyncEngine(db)
        return await engine.export_apple_csv(output, bitwarden_csv)

    try:
        count = asyncio.run(run_export())

        if count == 0:
            console.print("[yellow]No new passwords found in Bitwarden[/yellow]")
            console.print("   All Bitwarden passwords already exist in Apple Passwords")
        else:
            console.print(f"[green]✅ Apple Passwords CSV generated[/green]")
            console.print(f"   File: {output}")
            console.print(f"   New entries: {count}")
            console.print(f"   Permissions: 0600 (owner read/write only)")

            # Security warning
            console.print(
                "\n[yellow]⚠️  SECURITY WARNING[/yellow]\n"
                "   Generated CSV contains plaintext passwords!\n"
                f"   1. Import to Apple Passwords immediately\n"
                f"   2. Delete file: rm {output}\n"
            )

            # Instructions
            console.print(
                "[dim]💡 Import to Apple Passwords:[/dim]\n"
                "   1. Open Passwords app\n"
                "   2. File → Import Passwords\n"
                f"   3. Select {output}\n"
                "   4. Delete both CSV files!"
            )

    except Exception as e:
        console.print(f"[red]Error exporting to Apple format: {e}[/red]")
        logging.exception("Export failed")
        raise typer.Exit(1)


@passwords_app.command(name="status")
def passwords_status(ctx: typer.Context) -> None:
    """Show password sync status."""
    import asyncio
    from datetime import datetime

    from ..utils.db import PasswordsDB

    cfg = ctx.obj["config"]

    console.print(Panel.fit("🔐 Password Sync Status", style="bold blue"))

    # Initialize database
    db_path = cfg.passwords_db_path
    db = PasswordsDB(db_path)

    async def get_status():
        await db.initialize()

        # Get statistics
        stats = await db.get_stats()

        # Get last syncs
        apple_import = await db.get_last_sync("apple_import")
        bitwarden_export = await db.get_last_sync("bitwarden_export")
        bitwarden_import = await db.get_last_sync("bitwarden_import")
        apple_export = await db.get_last_sync("apple_export")

        return {
            "stats": stats,
            "apple_import": apple_import,
            "bitwarden_export": bitwarden_export,
            "bitwarden_import": bitwarden_import,
            "apple_export": apple_export,
        }

    try:
        data = asyncio.run(get_status())
        stats = data["stats"]

        # Display statistics
        table = Table(title="Database Statistics")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right", style="green")

        table.add_row("Total entries", str(stats["total"]))
        for source, count in stats["by_source"].items():
            table.add_row(f"  From {source}", str(count))

        console.print(table)

        # Display last syncs
        def format_timestamp(ts: float | None) -> str:
            if ts is None:
                return "Never"
            dt = datetime.fromtimestamp(ts)
            now = datetime.now()
            delta = now - dt
            if delta.days > 0:
                return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} ({delta.days} days ago)"
            elif delta.seconds > 3600:
                hours = delta.seconds // 3600
                return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} ({hours} hours ago)"
            else:
                minutes = delta.seconds // 60
                return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} ({minutes} minutes ago)"

        sync_table = Table(title="Last Syncs")
        sync_table.add_column("Operation", style="cyan")
        sync_table.add_column("Timestamp", style="yellow")

        sync_table.add_row(
            "Apple import",
            format_timestamp(data["apple_import"]["timestamp"] if data["apple_import"] else None),
        )
        sync_table.add_row(
            "Bitwarden export",
            format_timestamp(data["bitwarden_export"]["timestamp"] if data["bitwarden_export"] else None),
        )
        sync_table.add_row(
            "Bitwarden import",
            format_timestamp(data["bitwarden_import"]["timestamp"] if data["bitwarden_import"] else None),
        )
        sync_table.add_row(
            "Apple export",
            format_timestamp(data["apple_export"]["timestamp"] if data["apple_export"] else None),
        )

        console.print(sync_table)

        console.print(f"\n[dim]Database: {db_path}[/dim]")

    except Exception as e:
        console.print(f"[red]Error retrieving status: {e}[/red]")
        logging.exception("Status failed")
        raise typer.Exit(1)


@passwords_app.command(name="set-bitwarden-credentials")
def passwords_set_vaultwarden_credentials(ctx: typer.Context) -> None:
    """Set Bitwarden/VaultWarden credentials in system keyring."""
    from getpass import getpass

    from ..utils.credentials import CredentialStore

    cfg = ctx.obj["config"]

    console.print(Panel.fit("🔐 VaultWarden Credentials Setup", style="bold blue"))

    # Get VaultWarden URL from config or prompt
    url = cfg.passwords.vaultwarden_url
    if not url:
        url = typer.prompt("VaultWarden URL (e.g., https://vault.example.com)")

        # Update config
        cfg.passwords.vaultwarden_url = url
        try:
            cfg.ensure_data_dir()
            config_path = cfg.general.data_dir / "config.toml"
            cfg.save_to_file(config_path)
            console.print(f"[dim]Saved URL to config: {config_path}[/dim]")
        except Exception as e:
            console.print(f"[yellow]Warning: Could not save URL to config: {e}[/yellow]")

    # Get email
    email = cfg.passwords.vaultwarden_email
    if not email:
        email = typer.prompt("VaultWarden Email")

    # Get password (securely)
    console.print("\n[dim]Enter VaultWarden password (input hidden):[/dim]")
    password = getpass("Password: ")
    password_confirm = getpass("Confirm password: ")

    if password != password_confirm:
        console.print("[red]❌ Passwords do not match[/red]")
        raise typer.Exit(1)

    # Optional: client ID and secret
    console.print("\n[dim]OAuth client ID and secret (optional, press Enter to skip):[/dim]")
    client_id = typer.prompt("Client ID", default="", show_default=False) or None
    client_secret = None
    if client_id:
        client_secret = getpass("Client Secret: ") or None

    # Store in keyring
    try:
        cred_store = CredentialStore()
        cred_store.set_vaultwarden_credentials(email, password, client_id, client_secret)

        console.print(f"\n[green]✅ VaultWarden credentials stored securely[/green]")
        console.print(f"   Email: {email}")
        console.print(f"   URL: {url}")
        if client_id:
            console.print(f"   Client ID: {client_id}")

        console.print("\n[dim]💡 Test connection with:[/dim]")
        console.print(f"   icloudbridge passwords sync --apple-csv <path/to/passwords.csv>")

    except Exception as e:
        console.print(f"[red]❌ Failed to store credentials: {e}[/red]")
        raise typer.Exit(1)


@passwords_app.command(name="delete-bitwarden-credentials")
def passwords_delete_vaultwarden_credentials(
    ctx: typer.Context,
    email: str | None = typer.Option(None, "--email", help="Bitwarden/VaultWarden email"),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation"),
) -> None:
    """Delete Bitwarden/VaultWarden credentials from system keyring."""
    from ..utils.credentials import CredentialStore

    cfg = ctx.obj["config"]

    # Get email from config if not provided
    if not email:
        email = cfg.passwords.vaultwarden_email
        if not email:
            console.print("[red]Email not specified and not found in config[/red]")
            console.print("[dim]Use --email or set ICLOUDBRIDGE_PASSWORDS__VAULTWARDEN_EMAIL[/dim]")
            raise typer.Exit(1)

    if not yes:
        confirmed = typer.confirm(f"Delete stored credentials for {email}?")
        if not confirmed:
            console.print("[dim]Cancelled[/dim]")
            raise typer.Exit(0)

    # Delete from keyring
    try:
        cred_store = CredentialStore()
        if cred_store.delete_vaultwarden_credentials(email):
            console.print(f"[green]✓ Credentials deleted for: {email}[/green]")
        else:
            console.print(f"[yellow]No credentials found for: {email}[/yellow]")
    except Exception as e:
        console.print(f"[red]Failed to delete credentials: {e}[/red]")
        raise typer.Exit(1)


@passwords_app.command(name="set-nextcloud-credentials")
def passwords_set_nextcloud_credentials(ctx: typer.Context) -> None:
    """Set Nextcloud Passwords credentials in system keyring."""
    from getpass import getpass

    from ..utils.credentials import CredentialStore

    cfg = ctx.obj["config"]

    console.print(Panel.fit("🔐 Nextcloud Passwords Credentials Setup", style="bold blue"))

    config_path = cfg.general.config_file or cfg.default_config_path

    # Get Nextcloud URL from config or prompt
    url = cfg.passwords.nextcloud_url
    if not url:
        url = typer.prompt("Nextcloud URL (e.g., https://cloud.example.com)")

        # Update config
        cfg.passwords.nextcloud_url = url
        try:
            cfg.ensure_data_dir()
            cfg.save_to_file(config_path)
            console.print(f"[dim]Saved URL to config: {config_path}[/dim]")
        except Exception as e:
            console.print(f"[yellow]Warning: Could not save URL to config: {e}[/yellow]")

    # Get username
    username = cfg.passwords.nextcloud_username
    if not username:
        username = typer.prompt("Nextcloud Username")
        cfg.passwords.nextcloud_username = username
        try:
            cfg.ensure_data_dir()
            cfg.save_to_file(config_path)
            console.print(f"[dim]Saved username to config: {config_path}[/dim]")
        except Exception as e:
            console.print(f"[yellow]Warning: Could not save username to config: {e}[/yellow]")

    # Get app password (securely)
    console.print("\n[dim]Enter Nextcloud App Password (not your regular password!):[/dim]")
    console.print("[dim]Generate one at: Settings → Security → Devices & sessions[/dim]\n")
    app_password = getpass("App Password: ")
    app_password_confirm = getpass("Confirm app password: ")

    if app_password != app_password_confirm:
        console.print("[red]❌ App passwords do not match[/red]")
        raise typer.Exit(1)

    # Store in keyring
    try:
        cred_store = CredentialStore()
        cred_store.set_nextcloud_credentials(username, app_password)

        console.print(f"\n[green]✅ Nextcloud credentials stored securely[/green]")
        console.print(f"   Username: {username}")
        console.print(f"   URL: {url}")

        console.print("\n[dim]💡 Set provider in config:[/dim]")
        console.print("   icloudbridge passwords provider nextcloud")
        console.print("\n[dim]💡 Test connection with:[/dim]")
        console.print(f"   icloudbridge passwords sync --apple-csv <path/to/passwords.csv>")

    except Exception as e:
        console.print(f"[red]❌ Failed to store credentials: {e}[/red]")
        raise typer.Exit(1)


@passwords_app.command(name="delete-nextcloud-credentials")
def passwords_delete_nextcloud_credentials(
    ctx: typer.Context,
    username: str | None = typer.Option(None, "--username", help="Nextcloud username"),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation"),
) -> None:
    """Delete Nextcloud Passwords credentials from system keyring."""
    from ..utils.credentials import CredentialStore

    cfg = ctx.obj["config"]

    # Get username from config if not provided
    if not username:
        username = cfg.passwords.nextcloud_username
        if not username:
            console.print("[red]Username not specified and not found in config[/red]")
            console.print("[dim]Use --username or set ICLOUDBRIDGE_PASSWORDS__NEXTCLOUD_USERNAME[/dim]")
            raise typer.Exit(1)

    if not yes:
        confirmed = typer.confirm(f"Delete stored credentials for {username}?")
        if not confirmed:
            console.print("[dim]Cancelled[/dim]")
            raise typer.Exit(0)

    # Delete from keyring
    try:
        cred_store = CredentialStore()
        if cred_store.delete_nextcloud_credentials(username):
            console.print(f"[green]✓ Credentials deleted for: {username}[/green]")
        else:
            console.print(f"[yellow]No credentials found for: {username}[/yellow]")
    except Exception as e:
        console.print(f"[red]Failed to delete credentials: {e}[/red]")
        raise typer.Exit(1)


@passwords_app.command(name="sync")
def passwords_sync(
    ctx: typer.Context,
    apple_csv: Path = typer.Option(..., help="Apple Passwords CSV export"),
    output: Path | None = typer.Option(None, "-o", "--output", help="Output path for Apple CSV (default: data_dir/apple-import.csv)"),
    bulk: bool = typer.Option(False, "--bulk", help="Use bulk import if supported by provider"),
) -> None:
    """Full auto-sync: Apple → Provider (push) and Provider → Apple (pull)."""
    import asyncio

    from ..core.passwords_sync import PasswordsSyncEngine
    from ..sources.passwords.providers import (
        NextcloudPasswordsProvider,
        VaultwardenProvider,
    )
    from ..utils.db import PasswordsDB

    cfg = ctx.obj["config"]

    console.print(Panel.fit("🔐 Password Full Auto-Sync", style="bold blue"))

    # Validate Apple CSV exists
    if not apple_csv.exists():
        console.print(f"[red]Error: Apple CSV not found: {apple_csv}[/red]")
        raise typer.Exit(1)

    # Get provider
    provider_name = cfg.passwords.provider
    console.print(f"[dim]Provider: {provider_name}[/dim]")

    # Create provider based on configuration
    provider = None
    if provider_name == "vaultwarden":
        # Get VaultWarden configuration
        url = cfg.passwords.vaultwarden_url
        email = cfg.passwords.vaultwarden_email

        if not url:
            console.print("[red]VaultWarden URL not configured[/red]")
            console.print("[dim]Set with: icloudbridge passwords set-bitwarden-credentials[/dim]")
            raise typer.Exit(1)

        if not email:
            console.print("[red]VaultWarden email not configured[/red]")
            console.print("[dim]Set with: icloudbridge passwords set-bitwarden-credentials[/dim]")
            raise typer.Exit(1)

        # Get credentials
        credentials = cfg.passwords.get_vaultwarden_credentials()
        if not credentials:
            console.print("[red]VaultWarden credentials not found[/red]")
            console.print("[dim]Set with: icloudbridge passwords set-bitwarden-credentials[/dim]")
            raise typer.Exit(1)

        provider = VaultwardenProvider(
            url=url,
            email=credentials["email"],
            password=credentials["password"],
            client_id=credentials.get("client_id"),
            client_secret=credentials.get("client_secret"),
            ssl_verify_cert=cfg.passwords.passwords_ssl_verify_cert,
        )

    elif provider_name == "nextcloud":
        # Get Nextcloud configuration
        url = cfg.passwords.nextcloud_url
        username = cfg.passwords.nextcloud_username

        if not url:
            console.print("[red]Nextcloud URL not configured[/red]")
            console.print("[dim]Set with: icloudbridge passwords set-nextcloud-credentials[/dim]")
            raise typer.Exit(1)

        if not username:
            console.print("[red]Nextcloud username not configured[/red]")
            console.print("[dim]Set with: icloudbridge passwords set-nextcloud-credentials[/dim]")
            raise typer.Exit(1)

        # Get credentials
        credentials = cfg.passwords.get_nextcloud_credentials()
        if not credentials:
            console.print("[red]Nextcloud credentials not found[/red]")
            console.print("[dim]Set with: icloudbridge passwords set-nextcloud-credentials[/dim]")
            raise typer.Exit(1)

        provider = NextcloudPasswordsProvider(
            url=url,
            username=credentials["username"],
            app_password=credentials["app_password"],
            ssl_verify_cert=cfg.passwords.passwords_ssl_verify_cert,
        )

    else:
        console.print(f"[red]Unknown provider: {provider_name}[/red]")
        console.print("[dim]Supported providers: bitwarden (Vaultwarden), nextcloud[/dim]")
        raise typer.Exit(1)

    # Initialize database
    db_path = cfg.passwords_db_path
    db = PasswordsDB(db_path)

    cfg.ensure_data_dir()
    default_output = cfg.general.data_dir / "apple-import.csv"
    output_path = output or default_output

    async def run_sync():
        await db.initialize()

        # Authenticate with provider
        console.print(f"[dim]Authenticating with {provider_name}...[/dim]")
        await provider.authenticate()

        # Run full sync
        engine = PasswordsSyncEngine(db)
        result = await engine.sync(
            apple_csv_path=apple_csv,
            provider=provider,
            output_apple_csv=output_path,
            simulate=False,
            run_push=True,
            run_pull=True,
            bulk_push=bulk,
        )

        # Close provider connection
        await provider.close()

        return result

    try:
        stats = asyncio.run(run_sync())

        # Display results
        provider_display = provider_name.title()
        console.print("\n" + "=" * 60)
        console.print(f"📤 [bold]Apple → {provider_display} (Push)[/bold]")
        console.print("=" * 60)

        push_table = Table(show_header=False)
        push_table.add_column("Metric", style="cyan")
        push_table.add_column("Count", justify="right", style="green")

        push_stats = stats["push"]
        push_table.add_row("Created", str(push_stats.get("created", 0)))
        push_table.add_row("Updated", str(push_stats.get("updated", 0)))
        push_table.add_row("Skipped (unchanged)", str(push_stats.get("skipped", 0)))
        if push_stats.get("failed", 0) > 0:
            push_table.add_row("Failed", str(push_stats["failed"]), style="red")

        console.print(push_table)

        console.print("\n" + "=" * 60)
        console.print(f"📥 [bold]{provider_display} → Apple (Pull)[/bold]")
        console.print("=" * 60)

        pull_stats = stats["pull"]
        new_entries = pull_stats.get("new_entries", 0)

        if new_entries > 0:
            console.print(f"[green]✅ Generated Apple CSV with {new_entries} new entries[/green]")
            console.print(f"   File: {pull_stats.get('download_path')}")

            console.print("\n[yellow]⚠️  Manual step required:[/yellow]")
            console.print(f"   1. Open Passwords app")
            console.print(f"   2. File → Import Passwords")
            console.print(f"   3. Select: {pull_stats.get('download_path')}")
            console.print(f"   4. Delete CSV file after import")
        else:
            console.print(f"[dim]No new passwords from {provider_display}[/dim]")

        console.print("\n" + "=" * 60)
        console.print(f"[bold green]✅ Sync complete in {stats['total_time']:.1f}s[/bold green]")
        console.print("=" * 60)

        # Security reminder
        console.print(
            "\n[yellow]⚠️  SECURITY REMINDER[/yellow]\n"
            "   Delete CSV files after import:\n"
            f"   → rm {apple_csv}"
        )
        if new_entries > 0:
            console.print(f"   → rm {pull_stats.get('download_path')}")

    except Exception as e:
        console.print(f"[red]❌ Sync failed: {e}[/red]")
        logging.exception("Sync failed")
        raise typer.Exit(1)


@passwords_app.command(name="reset")
def passwords_reset(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation prompt"),
) -> None:
    """Clear all password entries from database."""
    import asyncio

    from ..utils.db import PasswordsDB

    cfg = ctx.obj["config"]

    if not yes:
        confirm = typer.confirm(
            "⚠️  This will delete all password entries from the database. Continue?"
        )
        if not confirm:
            console.print("[yellow]Cancelled[/yellow]")
            raise typer.Exit(0)

    db_path = cfg.passwords_db_path
    db = PasswordsDB(db_path)

    async def reset():
        await db.initialize()
        await db.clear_all_entries()

    try:
        asyncio.run(reset())
        console.print("[green]✅ Password database reset complete[/green]")
    except Exception as e:
        console.print(f"[red]Error resetting database: {e}[/red]")
        logging.exception("Reset failed")
        raise typer.Exit(1)


# Register passwords subcommand group
app.add_typer(passwords_app, name="passwords")


# =============================================================================
# Server Commands (Phase 1.6)
# =============================================================================


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host", "-h", help="Host to bind to")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Port to bind to")] = 8000,
    reload: Annotated[bool, typer.Option("--reload", help="Enable auto-reload (development)")] = False,
    background: Annotated[bool, typer.Option("--background", "-d", help="Run in background (daemon mode)")] = False,
) -> None:
    """Start the iCloudBridge API server.

    This command starts the FastAPI server that provides the web UI and REST API.

    Examples:
        # Start server on default port
        icloudbridge serve

        # Start on specific host and port
        icloudbridge serve --host 0.0.0.0 --port 8080

        # Start in background
        icloudbridge serve --background

        # Development mode with auto-reload
        icloudbridge serve --reload
    """
    import uvicorn

    console.print(Panel.fit(
        f"[bold cyan]iCloudBridge API Server[/bold cyan]\n\n"
        f"[white]Starting server on {host}:{port}[/white]",
        border_style="cyan"
    ))

    if background:
        # Run in background mode
        import subprocess
        import sys

        # Get the path to this script
        script_path = sys.argv[0]

        # Create command without --background flag
        cmd = [
            sys.executable,
            script_path,
            "serve",
            "--host", host,
            "--port", str(port),
        ]

        if reload:
            cmd.append("--reload")

        # Start background process
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        console.print(f"[green]✅ Server started in background (PID: {process.pid})[/green]")
        console.print(f"[dim]Access the API at: http://{host}:{port}/api/docs[/dim]")
        return

    # Run in foreground
    try:
        console.print(f"[green]Server running at: http://{host}:{port}[/green]")
        console.print(f"[dim]API docs: http://{host}:{port}/api/docs[/dim]")
        console.print(f"[dim]Press Ctrl+C to stop[/dim]\n")

        uvicorn.run(
            "icloudbridge.api.app:app",
            host=host,
            port=port,
            reload=reload,
            log_level="info",
            log_config=None,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Server stopped[/yellow]")




# Service management subcommand group
service_app = typer.Typer(help="Manage the iCloudBridge service")
app.add_typer(service_app, name="service")


@service_app.command("install")
def service_install(
    port: Annotated[int, typer.Option("--port", help="Port for API server")] = 8000,
    start_on_boot: Annotated[bool, typer.Option("--start-on-boot", help="Start on login")] = True,
) -> None:
    """Install iCloudBridge as a macOS LaunchAgent service.

    This creates a launchd plist file that starts the API server automatically.

    Examples:
        # Install with default settings
        icloudbridge service install

        # Install without auto-start on boot
        icloudbridge service install --no-start-on-boot

        # Install on custom port
        icloudbridge service install --port 8080
    """
    import plistlib
    import subprocess
    from pathlib import Path

    # LaunchAgents directory
    launch_agents_dir = Path.home() / "Library" / "LaunchAgents"
    launch_agents_dir.mkdir(parents=True, exist_ok=True)

    plist_path = launch_agents_dir / "com.icloudbridge.server.plist"

    # Check if already installed
    if plist_path.exists():
        console.print("[yellow]⚠️  Service already installed[/yellow]")
        if not typer.confirm("Overwrite existing service?"):
            raise typer.Abort()

    # Get paths
    python_path = sys.executable
    cli_module = "icloudbridge.cli.main"

    # Create plist
    plist = {
        "Label": "com.icloudbridge.server",
        "ProgramArguments": [
            python_path,
            "-m",
            cli_module,
            "serve",
            "--port", str(port),
        ],
        "RunAtLoad": start_on_boot,
        "KeepAlive": True,
        "StandardOutPath": str(Path.home() / "Library" / "Logs" / "iCloudBridge" / "stdout.log"),
        "StandardErrorPath": str(Path.home() / "Library" / "Logs" / "iCloudBridge" / "stderr.log"),
        "WorkingDirectory": str(Path.home()),
    }

    # Create log directory
    log_dir = Path.home() / "Library" / "Logs" / "iCloudBridge"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Write plist file
    with open(plist_path, "wb") as f:
        plistlib.dump(plist, f)

    console.print(f"[green]✅ Service installed at: {plist_path}[/green]")

    # Load the service
    if start_on_boot:
        try:
            subprocess.run(
                ["launchctl", "load", str(plist_path)],
                check=True,
                capture_output=True,
            )
            console.print("[green]✅ Service loaded and started[/green]")
            console.print(f"[dim]API accessible at: http://127.0.0.1:{port}/api/docs[/dim]")
        except subprocess.CalledProcessError as e:
            console.print(f"[red]Failed to load service: {e.stderr.decode()}[/red]")
    else:
        console.print("[dim]Service installed but not loaded (use 'service start' to start)[/dim]")


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Uninstall the iCloudBridge LaunchAgent service.

    Examples:
        icloudbridge service uninstall
    """
    import subprocess
    from pathlib import Path

    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.icloudbridge.server.plist"

    if not plist_path.exists():
        console.print("[yellow]Service not installed[/yellow]")
        return

    # Unload the service
    try:
        subprocess.run(
            ["launchctl", "unload", str(plist_path)],
            check=True,
            capture_output=True,
        )
        console.print("[green]✅ Service unloaded[/green]")
    except subprocess.CalledProcessError:
        # Service might not be loaded, continue anyway
        pass

    # Remove plist file
    plist_path.unlink()
    console.print(f"[green]✅ Service uninstalled[/green]")


@service_app.command("status")
def service_status() -> None:
    """Check if the iCloudBridge service is running.

    Examples:
        icloudbridge service status
    """
    import subprocess
    from pathlib import Path

    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.icloudbridge.server.plist"

    if not plist_path.exists():
        console.print("[yellow]Service not installed[/yellow]")
        console.print("[dim]Run 'icloudbridge service install' to install[/dim]")
        return

    # Check service status
    try:
        result = subprocess.run(
            ["launchctl", "list", "com.icloudbridge.server"],
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            console.print("[green]✅ Service is running[/green]")
            # Parse output to get PID
            for line in result.stdout.split("\n"):
                if "PID" in line:
                    console.print(f"[dim]{line}[/dim]")
        else:
            console.print("[yellow]Service is installed but not running[/yellow]")
            console.print("[dim]Run 'icloudbridge service start' to start[/dim]")

    except Exception as e:
        console.print(f"[red]Error checking service status: {e}[/red]")


@service_app.command("start")
def service_start() -> None:
    """Start the iCloudBridge service.

    Examples:
        icloudbridge service start
    """
    import subprocess
    from pathlib import Path

    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.icloudbridge.server.plist"

    if not plist_path.exists():
        console.print("[red]Service not installed[/red]")
        console.print("[dim]Run 'icloudbridge service install' first[/dim]")
        raise typer.Exit(1)

    try:
        subprocess.run(
            ["launchctl", "load", str(plist_path)],
            check=True,
            capture_output=True,
        )
        console.print("[green]✅ Service started[/green]")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Failed to start service: {e.stderr.decode()}[/red]")
        raise typer.Exit(1)


@service_app.command("stop")
def service_stop() -> None:
    """Stop the iCloudBridge service.

    Examples:
        icloudbridge service stop
    """
    import subprocess
    from pathlib import Path

    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.icloudbridge.server.plist"

    if not plist_path.exists():
        console.print("[red]Service not installed[/red]")
        raise typer.Exit(1)

    try:
        subprocess.run(
            ["launchctl", "unload", str(plist_path)],
            check=True,
            capture_output=True,
        )
        console.print("[green]✅ Service stopped[/green]")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Failed to stop service: {e.stderr.decode()}[/red]")
        raise typer.Exit(1)


@service_app.command("restart")
def service_restart() -> None:
    """Restart the iCloudBridge service.

    Examples:
        icloudbridge service restart
    """
    console.print("[cyan]Restarting service...[/cyan]")
    service_stop()
    import time
    time.sleep(1)
    service_start()
    console.print("[green]✅ Service restarted[/green]")


def main_entry() -> None:
    """Entry point for the CLI."""
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        sys.exit(130)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        logging.exception("Unhandled exception")
        sys.exit(1)


if __name__ == "__main__":
    main_entry()
