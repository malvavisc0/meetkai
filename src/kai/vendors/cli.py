"""``kai vendors`` CLI — install/update/delete external vendor dependencies.

Replaces ``scripts/setup_media.sh`` and ``scripts/setup_kokoro.sh`` with a
single Python command so the same code runs locally, in CI, and in the
container without a shell layer.
"""

import logging

import typer
from rich.console import Console

from kai.cli.style import soft_table
from kai.vendors import VENDOR_NAMES, get_vendor_manager

app = typer.Typer(
    name="vendors",
    no_args_is_help=True,
    help="Install/update/delete external vendor deps (ffmpeg, whisper.cpp, kokoro).",
)
console = Console()
logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    # Vendor installers log via the stdlib logger; surface INFO to the console
    # so progress is visible without configuring the full kai logging stack.
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")


@app.command("list")
def vendors_list() -> None:
    """Show installed vendors, versions, and paths."""
    mgr = get_vendor_manager()
    table = soft_table(
        ("vendor", ""),
        ("installed", ""),
        ("version", "dim"),
        ("vendor dir", "dim"),
        ("model dir", "dim"),
    )
    for row in mgr.status_rows():
        installed = "[green]✓ yes[/green]" if row["installed"] else "[red]✗ no[/red]"
        table.add_row(
            row["name"],
            installed,
            row["version"] or "-",
            row["vendor_dir"],
            row["model_dir"],
        )
    console.print(table)


_VENDOR_HELP = f"Vendor name or 'all' ({', '.join(VENDOR_NAMES)}, all)"


@app.command("install")
def vendors_install(
    vendor: str = typer.Argument(..., help=_VENDOR_HELP),
) -> None:
    """Install (or reinstall) a vendor's binaries + models."""
    _setup_logging()
    mgr = get_vendor_manager()
    try:
        results = mgr.install(vendor)
    except KeyError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(1) from exc
    for r in results:
        if r.ok:
            console.print(f"[green]✓ installed[/green] {r.name}: {r.path}")
            if r.detail:
                console.print(f"  [dim]{r.detail}[/dim]")
        else:
            console.print(f"[red]✗ failed[/red] {r.name}: {r.detail}")
            raise typer.Exit(1)


@app.command("update")
def vendors_update(
    vendor: str = typer.Argument(..., help=_VENDOR_HELP),
) -> None:
    """Update a vendor (re-fetch/rebuild to the pinned version)."""
    _setup_logging()
    mgr = get_vendor_manager()
    try:
        results = mgr.update(vendor)
    except KeyError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(1) from exc
    for r in results:
        mark = "[green]✓ updated[/green]" if r.ok else "[red]✗ failed[/red]"
        console.print(f"{mark} {r.name}: {r.path or r.detail}")


@app.command("delete")
def vendors_delete(
    vendor: str = typer.Argument(..., help=_VENDOR_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Remove a vendor's binaries AND its downloaded models."""
    mgr = get_vendor_manager()
    if not yes:
        confirm = typer.confirm(
            f"Delete vendor '{vendor}' and all its models? This cannot be undone.",
            default=False,
        )
        if not confirm:
            console.print("[yellow]● aborted[/yellow]")
            raise typer.Exit(0)
    try:
        results = mgr.delete(vendor)
    except KeyError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(1) from exc
    for r in results:
        if r.ok:
            console.print(f"[green]✓ deleted[/green] {r.name}: {r.detail}")
        else:
            console.print(f"[red]✗ failed[/red] {r.name}: {r.detail}")
            raise typer.Exit(1)
