from rich.align import Align

from kai.cli.style import (
    ACCENT,
    DIM,
    ERR,
    GL_ERR,
    GL_OK,
    OK,
    console,
    err_line,
    soft_table,
)


def render_status(data: dict, *, uptime: int | None = None) -> None:
    """Render a status snapshot dict (from the ``/status`` route) as bullets."""
    lines: list = []

    if uptime is not None:
        lines.append(format_uptime_line(uptime))

    lines.extend(format_tasks_lines(data.get("tasks")))
    lines.extend(format_caps_lines(data.get("capabilities")))

    from rich.console import Group

    console.print(Group(*(Align.left(line) for line in lines)))


def format_uptime_line(uptime: int) -> str:
    """Format an uptime in seconds as a single status line."""
    days, rem = divmod(uptime, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        dur = f"{days}d {hours}h {minutes}m"
    elif hours:
        dur = f"{hours}h {minutes}m"
    elif minutes:
        dur = f"{minutes}m {seconds}s"
    else:
        dur = f"{seconds}s"
    return f"[blue]\u25cf[/blue] [bold]uptime[/bold]  [{DIM}]{dur}  ({uptime}s)[/{DIM}]"


def format_tasks_lines(tasks: dict | None) -> list[str]:
    """Format the status task summary and its first five items."""
    if not tasks:
        return []
    pending = tasks.get("pending", 0)
    recurring = tasks.get("recurring", 0)
    total = pending + recurring
    if not total:
        return [f"[{OK}]\u25cf[/{OK}] [bold]tasks[/bold]  [{DIM}]none[/{DIM}]"]
    parts = []
    if pending:
        parts.append(f"{pending} pending")
    if recurring:
        parts.append(f"{recurring} recurring")
    lines = [f"[cyan]\u25cf[/cyan] [bold]tasks[/bold]  [{DIM}]{', '.join(parts)}[/{DIM}]"]
    for item in tasks.get("items", [])[:5]:
        repeat = item.get("repeat", "none")
        tag = f" ({repeat})" if repeat != "none" else ""
        lines.append(f"   [{DIM}]{item.get('goal', '?')[:60]}{tag}[/{DIM}]")
    if total > 5:
        lines.append(f"   [{DIM}]\u2026 +{total - 5} more[/{DIM}]")
    return lines


def format_caps_lines(caps: dict | None) -> list[str]:
    """Format the status capability flags as a single line."""
    if not caps:
        return []
    flags = []
    for key, label in [("vision", "vision")]:
        flags.append(f"[{OK}]{label}[/{OK}]" if caps.get(key) else f"[{DIM}]{label}[/{DIM}]")
    return ["[bold]capabilities[/bold]  " + "  ".join(flags)]


def render_tell(data: dict) -> None:
    """Render a TellResult dict as a compact, styled summary."""
    if not isinstance(data, dict):
        err_line("unexpected reply from bot (expected a JSON object)")
        return

    ok = data.get("ok", False)
    reply = data.get("reply", "") or "(no reply)"

    mark = GL_OK if ok else GL_ERR
    label = f"[{OK}]reply[/{OK}]" if ok else f"[{ERR}]error[/{ERR}]"
    console.print(f"{mark} {label}")
    console.print(f"  {reply}")

    actions = data.get("actions") or []
    if not actions:
        return
    table = soft_table(
        ("", ""),
        ("tool", ACCENT),
        ("target", DIM),
        ("text", ""),
        ("status", ""),
    )
    for a in actions:
        table.add_row(*render_tell_row(a))
    console.print(table)


def render_tell_row(a: dict) -> tuple[str, str, str, str, str]:
    """Render one TellResult action as a table row tuple."""
    name = a.get("tool", "?")
    a_ok = a.get("ok", False)
    mark = GL_OK if a_ok else GL_ERR
    target = first_nonempty(a, ("chat_id", "target"))
    target = truncate(target, 40) if target else f"[{DIM}]\u2014[/{DIM}]"
    text = first_nonempty(a, ("text", "goal")) or first_other_arg(a)
    text = truncate(text, 80) if text else f"[{DIM}]\u2014[/{DIM}]"
    status = f"[{OK}]ok[/{OK}]" if a_ok else f"[{ERR}]failed[/{ERR}]"
    return mark, name, target, text, status


def first_nonempty(a: dict, keys: tuple[str, ...]) -> str:
    """Return the first non-empty value among the given keys."""
    for key in keys:
        val = a.get(key)
        if val:
            return str(val)
    return ""


def first_other_arg(a: dict) -> str:
    """Return the first non-empty action value that is not a known field."""
    for key, val in a.items():
        if key in ("tool", "ok", "target", "chat_id"):
            continue
        if val is None or val == "" or val is False:
            continue
        return str(val)
    return ""


def truncate(text: str, limit: int) -> str:
    """Truncate text to the limit, replacing the tail with an ellipsis."""
    return text[: limit - 3] + "..." if len(text) > limit else text


def render_help() -> None:
    """Render the slash-command list shown by ``/help``."""
    table = soft_table(("command", "magenta"), ("description", DIM))
    table.add_row("/help", "list these commands")
    table.add_row("/quit /exit", "leave the chat")
    table.add_row("/persist", "toggle persistent changes (set_goal)")
    table.add_row("/clear", "reset the operator conversation history")
    console.print(table)
