from datetime import UTC, datetime

from rich.box import ROUNDED
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# --- CLI style tokens -------------------------------------------------------
# A tiny, consistent visual language so every command looks the same:
#   • borders are dropped in favor of whitespace + color + glyph prefixes
#   • panels survive only for card-like outputs (run id) and fatal errors
OK, WARN, ERR, ACCENT, DIM = "green", "yellow", "red", "cyan", "dim"
GL_OK = f"[{OK}]\u2713[/{OK}]"  # ✓
GL_ERR = f"[{ERR}]\u2717[/{ERR}]"  # ✗
GL_RUN = f"[{ACCENT}]\u25cf[/{ACCENT}]"  # ● running
GL_IDLE = f"[{DIM}]\u25cb[/{DIM}]"  # ○ idle
GL_ARROW = f"[{DIM}]\u2192[/{DIM}]"  # →


def soft_table(*columns: tuple[str, str], header: bool = True) -> Table:
    """Borderless, modern table: no box, no vertical rules, just spacing.

    Columns are (name, style) tuples; pass style="" for default. A single dim
    header replaces the heavy box-drawing top border.
    """
    table = Table(box=None, show_header=header, show_edge=False, padding=(0, 2))
    table.header_style = f"bold {DIM}"
    for name, style in columns:
        table.add_column(name, style=style or None, no_wrap=False)
    return table


def card(title: str, body: str, *, border: str = ACCENT) -> None:
    """A thin rounded panel used for card-like outputs (run id, fatal errors)."""
    console.print(
        Panel(
            body,
            title=title,
            border_style=border,
            box=ROUNDED,
            padding=(0, 1),
            expand=False,
        )
    )


def err_line(message: str, *, hint: str = "") -> None:
    """Single-line error: `✗ error   <message> [· hint]`."""
    extra = f"  [{DIM}]\u00b7 {hint}[/{DIM}]" if hint else ""
    console.print(f"{GL_ERR} [{ERR}]error[/{ERR}]  {message}{extra}")


class BotStartupError(Exception):
    """Raised when a bot fails during async startup."""


def _uptime_seconds(started_at: str) -> int | None:
    """Seconds since the run's ``started_at`` ISO timestamp, or None."""
    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(started_at)
        now = datetime.now(UTC)
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return max(0, int((now - started).total_seconds()))
    except (ValueError, TypeError):
        return None


def _relative(started_at: str) -> str:
    """Compact '2m ago' / '3h ago' / 'just now' from an ISO timestamp."""
    secs = _uptime_seconds(started_at)
    if secs is None:
        return ""
    if secs < 5:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"
