import asyncio
import json
import logging
import os
import signal

import httpx
import typer

from kai.agent.core import KaiAgent
from kai.agent.goal import GoalManager
from kai.bots import list_bots, load_bot
from kai.cli.bot_display import render_help, render_status, render_tell
from kai.cli.bot_runtime import load_startup_template, run_instance
from kai.cli.style import (
    DIM,
    GL_ARROW,
    GL_IDLE,
    GL_OK,
    GL_RUN,
    OK,
    WARN,
    console,
    err_line,
    soft_table,
    uptime_seconds,
)
from kai.config.settings import Settings, get_settings
from kai.logging.logger import setup_logging
from kai.runs import (
    RunRecord,
    RunRegistry,
    runs_path,
    sanitize_instance_id,
)
from kai.utils.common import compute_hmac

logger = logging.getLogger(__name__)


def runs_registry(bot_name: str, settings: Settings) -> RunRegistry:
    """Open the run registry for a bot instance."""
    return RunRegistry(runs_path(settings.agent_history_folder, bot_name))


def bot_instance_id(bot_name: str, user: str = "") -> str:
    """Per-user instance id matching what `start` registers under."""
    raw = f"{bot_name}-{user}" if user else bot_name
    return sanitize_instance_id(raw)


def resolve_run(bot_name: str, run_id: str, user: str = "") -> RunRecord:
    """Resolve a run_id to the live RunRecord or exit with an error panel."""
    settings = get_settings()
    setup_logging()
    instance_id = bot_instance_id(bot_name, user)
    record = runs_registry(instance_id, settings).get()
    if record is None or record.run_id != run_id:
        err_line(
            f"unknown or stale run: {run_id}",
            hint=f"check the run id printed by `kai bot start {bot_name}`",
        )
        raise typer.Exit(1)
    return record


def post_tell(record: RunRecord, message: str, *, persist: bool) -> tuple[int, dict]:
    """POST {message, persist} to the run's /tell route, return (status, data)."""
    body = json.dumps({"message": message, "persist": persist}).encode("utf-8")
    signature = compute_hmac(record.hmac_key, body, record.hmac_algorithm)
    resp = httpx.post(
        f"{record.endpoint}/tell",
        content=body,
        headers={"Content-Type": "application/json", "X-Webhook-Hmac": signature},
        timeout=120.0,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"ok": False, "reply": resp.text}
    return resp.status_code, data


def post_clear(record: RunRecord) -> tuple[int, dict]:
    """POST an HMAC-signed empty body to the run's /clear route."""
    signature = compute_hmac(record.hmac_key, b"", record.hmac_algorithm)
    resp = httpx.post(
        f"{record.endpoint}/clear",
        content=b"",
        headers={"X-Webhook-Hmac": signature},
        timeout=30.0,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"ok": False, "reply": resp.text}
    return resp.status_code, data


def start_bot(
    bot_name: str,
    goal_text: str,
    language: str,
    user: str,
    template_name: str,
    enable_tools: str,
    disable_tools: str,
) -> None:
    """Start a bot. Blocks until SIGINT/SIGTERM."""
    settings = get_settings()
    setup_logging(log_dir=settings.log_dir)

    if language:
        settings.agent_language = language
        settings.agent_language_explicit = True

    try:
        bot = load_bot(bot_name)
    except ValueError as exc:
        err_line(str(exc))
        raise typer.Exit(1) from exc

    # Resolve the template + the final tool set. ``general`` is the default
    # and reproduces today's behavior.
    tmpl, tool_resolution = load_startup_template(
        bot_name, template_name, enable_tools, disable_tools
    )

    # Instance namespace: when --user is provided, isolate files per user.
    instance_id = bot_instance_id(bot_name, user)

    if user:
        bot.instance = instance_id

    agent = KaiAgent(
        settings=settings,
        goal_manager=GoalManager(),
        namespace=instance_id,
        escalations=bot.escalations,
    )
    if goal_text:
        agent.goal_manager.set_goal(goal_text)

    console.print(
        f"[bold magenta]kai[/bold magenta] [{DIM}]v0.0.1[/{DIM}]  {GL_ARROW} "
        f"[{DIM}]starting[/{DIM}] [bold]{bot.name}[/bold]  "
        f"[{DIM}]{tmpl.transport}/{tmpl.name}[/{DIM}]"
    )

    exit_code = asyncio.run(
        run_instance(
            bot,
            agent,
            tmpl,
            tool_resolution,
            instance_id,
            bot_name,
            user,
            settings,
            runs_registry,
        )
    )
    if exit_code:
        raise typer.Exit(exit_code)


def list_available_bots() -> None:
    """List available bots."""
    bots = list_bots()
    if not bots:
        console.print(f"[{DIM}]no bots found[/{DIM}]")
        return
    console.print(f"[bold]bots[/bold]  [{DIM}]{len(bots)} available[/{DIM}]")
    table = soft_table(("name", "magenta"), ("status", ""))
    for name in bots:
        table.add_row(name, f"[{OK}]available[/{OK}]")
    console.print(table)


def status_bot(
    bot_name: str,
    run_id: str,
    user: str,
) -> None:
    """Show a running bot's transport/session status.

    Targets a specific run (from `kai bot start` output), not a bot name: it
    GETs the run's ``/status`` route and renders the snapshot the bot returns.
    """
    record = resolve_run(bot_name, run_id, user=user)

    signature = compute_hmac(record.hmac_key, b"", record.hmac_algorithm)

    try:
        resp = httpx.get(
            f"{record.endpoint}/status",
            headers={"X-Webhook-Hmac": signature},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        err_line(f"failed to reach {record.endpoint}  {exc}")
        raise typer.Exit(1) from exc

    if resp.status_code != 200:
        err_line(f"status failed ({resp.status_code})  {resp.text}")
        raise typer.Exit(1)

    try:
        data = resp.json()
    except Exception as exc:
        err_line(f"invalid status response  {exc}")
        raise typer.Exit(1) from exc

    render_status(data, uptime=uptime_seconds(record.started_at))


def tell_bot(
    bot_name: str,
    run_id: str,
    message: str,
    persist: bool,
    user: str,
) -> None:
    """Send an instruction to a running bot via its /tell route.

    Targets a specific run (from `kai bot start` output), not a bot name. The
    CLI is bot-agnostic: it forwards ``{message, persist}`` verbatim and
    prints the structured ``TellResult`` the bot returns.
    """
    record = resolve_run(bot_name, run_id, user=user)

    try:
        status_code, data = post_tell(record, message, persist=persist)
    except httpx.HTTPError as exc:
        err_line(f"failed to reach {record.endpoint}  {exc}")
        raise typer.Exit(1) from exc

    # Drop empty envelope fields so the printed result stays compact (a
    # tell with no actions/reply shouldn't show null/[] lines).
    if isinstance(data, dict):
        data = {
            k: v
            for k, v in data.items()
            if not (v is None or (isinstance(v, (list, str)) and not v))
        }
        data.setdefault("ok", False)

    render_tell(data)
    ok = isinstance(data, dict) and data.get("ok")
    if status_code != 200 or not ok:
        raise typer.Exit(1)


def chat_bot(
    bot_name: str,
    run_id: str,
    persist: bool,
    user: str,
) -> None:
    """Interactive multiturn chat with a running bot.

    A client-side loop over the existing ``/tell`` route. The server is the
    source of truth for conversation history (the ``operator`` bucket
    accumulates turns across the session), so the CLI keeps no local state
    beyond the ``persist`` toggle. Slash commands act locally; everything
    else is forwarded as an operator turn.
    """
    record = resolve_run(bot_name, run_id, user=user)

    console.print(
        f"[bold magenta]kai chat[/bold magenta] [{DIM}]{bot_name} \u00b7 {run_id}[/{DIM}]"
    )
    console.print(f"[{DIM}]/help for commands \u00b7 Ctrl+D to quit[/{DIM}]")
    console.rule(style="dim")

    local_persist = persist

    while True:
        tag = persist_tag(local_persist)
        try:
            message = console.input(f"{tag} [bold cyan]\u203a[/bold cyan] ")
        except (EOFError, KeyboardInterrupt):
            console.print(f"{GL_IDLE} [{DIM}]bye[/{DIM}]")
            return

        message = message.strip()
        if not message:
            continue

        if message in ("/quit", "/exit"):
            console.print(f"{GL_IDLE} [{DIM}]bye[/{DIM}]")
            return

        if message.startswith("/"):
            local_persist = handle_slash(message, local_persist, record)
            continue

        data = post_and_clean_tell(record, message, local_persist)
        if data is not None:
            render_tell(data)


def post_and_clean_tell(record: RunRecord, message: str, persist: bool) -> dict | None:
    """POST a /tell and return cleaned data, or None on failure."""
    try:
        with console.status(f"[{DIM}]thinking...[/{DIM}]", spinner="dots"):
            _status_code, data = post_tell(record, message, persist=persist)
    except httpx.HTTPError as exc:
        err_line(f"failed to reach {record.endpoint}  {exc}")
        return None
    if isinstance(data, dict):
        data = {
            k: v
            for k, v in data.items()
            if not (v is None or (isinstance(v, (list, str)) and not v))
        }
        data.setdefault("ok", False)
    return data


def persist_tag(local_persist: bool) -> str:
    """Render the chat prompt tag showing the persist toggle state."""
    if local_persist:
        return "[bold green]\u25cfpersist[/bold green]"
    return f"[{DIM}]\u25cb[/{DIM}]"


def handle_slash(message: str, local_persist: bool, record: RunRecord) -> bool:
    """Handle a slash command (except /quit, handled by the caller).
    Returns the (possibly toggled) ``local_persist`` value."""
    if message == "/help":
        render_help()
        return local_persist
    if message == "/persist":
        local_persist = not local_persist
        state = "on" if local_persist else "off"
        console.print(f"[{DIM}]persist[/{DIM}] [bold]{state}[/bold]")
        return local_persist
    if message == "/clear":
        do_clear(record)
    return local_persist


def do_clear(record: RunRecord) -> None:
    """Clear the remote run's operator conversation history."""
    try:
        status_code, data = post_clear(record)
    except httpx.HTTPError as exc:
        err_line(f"failed to reach {record.endpoint}  {exc}")
        return
    ok = status_code == 200 and isinstance(data, dict) and data.get("ok")
    if ok:
        console.print(f"{GL_OK} [{OK}]clear[/{OK}]  [{DIM}]history cleared[/{DIM}]")
    elif isinstance(data, dict):
        err_line(data.get("error", "clear failed"))
    else:
        err_line("clear failed")


def stop_bot(
    bot_name: str,
    run_id: str,
    force: bool,
    user: str,
) -> None:
    """Stop a running bot instance.

    Resolves a ``run_id`` (from `kai bot start`) to its process and sends it a
    signal: SIGTERM by default, which the bot's signal handler turns into a
    graceful shutdown (flushes history, unregisters the run). ``--force``
    sends SIGKILL — use only if the bot is wedged; the bot can't unregister
    its run record then, so it is pruned here instead.
    """
    instance_id = bot_instance_id(bot_name, user)
    record = resolve_run(bot_name, run_id, user=user)

    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(record.pid, sig)
    except ProcessLookupError:
        # Already gone — drop the stale record so it can't be reused.
        runs_registry(instance_id, get_settings()).remove()
        console.print(
            f"[{WARN}]\u25cf[/{WARN}] [{WARN}]stopped[/{WARN}]  run {run_id} was already gone"
        )
        return
    except PermissionError as exc:
        err_line(f"permission denied signaling pid {record.pid}  {exc}")
        raise typer.Exit(1) from exc

    verb = "killed" if force else "stopping"
    console.print(
        f"{GL_RUN} [{WARN}]{verb}[/{WARN}]  [bold yellow]{run_id}[/bold yellow]  "
        f"[{DIM}]pid {record.pid} \u00b7 {sig.name}[/{DIM}]"
    )


def build_app() -> typer.Typer:
    """Build the ``bot`` sub-app with all lifecycle commands registered."""
    bot_app = typer.Typer(
        name="bot",
        no_args_is_help=True,
        help="Start, inspect, and stop bot instances.",
    )
    add_commands(bot_app)
    return bot_app


def add_commands(bot_app: typer.Typer, *, hidden: bool = False) -> None:
    """Register the bot lifecycle commands on the given typer app.

    Called for the ``kai bot`` sub-app and again with ``hidden=True`` for the
    hidden top-level aliases (``kai start`` etc.) kept for back-compat. A
    fresh call re-creates each command's option objects.
    """

    @bot_app.command("start", hidden=hidden)
    def start(
        bot_name: str = typer.Argument(..., help="Bot to start, e.g. email"),
        goal_text: str = typer.Option("", "--goal", "-g", help="Goal to give the bot at startup"),
        language: str = typer.Option("", "--language", "-l", help="Language override"),
        user: str = typer.Option(
            "", "--user", "-u", help="User email; keeps each user's runs separate"
        ),
        template: str = typer.Option(
            "general",
            "--template",
            "-t",
            help="Template to start from (default: general)",
        ),
        enable_tools: str = typer.Option(
            "",
            "--enable-tools",
            help="Extra tools to turn on, comma-separated",
        ),
        disable_tools: str = typer.Option(
            "",
            "--disable-tools",
            help="Tools to turn off, comma-separated",
        ),
    ) -> None:
        """Start a bot and serve its webhook endpoint.

        It resolves the template and tool set, then keeps running until you
        stop it. Pass the printed run id to the status, tell, chat, and stop
        commands.
        """
        start_bot(
            bot_name,
            goal_text,
            language,
            user,
            template,
            enable_tools,
            disable_tools,
        )

    @bot_app.command("list", hidden=hidden)
    def list_cmd() -> None:
        """List the bots you can start."""
        list_available_bots()

    @bot_app.command("status", hidden=hidden)
    def status(
        bot_name: str = typer.Argument(..., help="Bot to query, e.g. email"),
        run_id: str = typer.Option(..., "--run", help="Run id printed by `kai bot start`"),
        user: str = typer.Option(
            "",
            "--user",
            "-u",
            help="User email the instance was started with",
        ),
    ) -> None:
        """Show what a running bot is doing."""
        status_bot(bot_name, run_id, user)

    @bot_app.command("tell", hidden=hidden)
    def tell(
        bot_name: str = typer.Argument(..., help="Bot to instruct, e.g. email"),
        message: str = typer.Option(..., "--message", "-m", help="Instruction to send"),
        run_id: str = typer.Option(..., "--run", help="Run id printed by `kai bot start`"),
        persist: bool = typer.Option(
            False,
            "--persist",
            help="Allow changes that persist, for example setting the goal",
        ),
        user: str = typer.Option(
            "",
            "--user",
            "-u",
            help="User email the instance was started with",
        ),
    ) -> None:
        """Send one instruction to a running bot."""
        tell_bot(bot_name, run_id, message, persist, user)

    @bot_app.command("chat", hidden=hidden)
    def chat(
        bot_name: str = typer.Argument(..., help="Bot to chat with, e.g. email"),
        run_id: str = typer.Option(..., "--run", help="Run id printed by `kai bot start`"),
        persist: bool = typer.Option(
            False, "--persist", help="Allow messages that change saved state"
        ),
        user: str = typer.Option(
            "",
            "--user",
            "-u",
            help="User email the instance was started with",
        ),
    ) -> None:
        """Chat with a running bot in your terminal."""
        chat_bot(bot_name, run_id, persist, user)

    @bot_app.command("stop", hidden=hidden)
    def stop(
        bot_name: str = typer.Argument(..., help="Bot to stop, e.g. email"),
        run_id: str = typer.Option(..., "--run", help="Run id printed by `kai bot start`"),
        force: bool = typer.Option(
            False, "--force", help="Kill immediately instead of shutting down gracefully"
        ),
        user: str = typer.Option(
            "",
            "--user",
            "-u",
            help="User email the instance was started with",
        ),
    ) -> None:
        """Stop a running bot (SIGTERM, or SIGKILL with --force)."""
        stop_bot(bot_name, run_id, force, user)


def register(app: typer.Typer) -> None:
    """Attach the ``bot`` sub-app plus hidden top-level aliases (back-compat)."""
    app.add_typer(build_app(), name="bot")
    add_commands(app, hidden=True)
