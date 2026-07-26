import asyncio
import json
import logging
import os
import signal

import httpx
import typer
from rich.align import Align

from kai.agent.core import KaiAgent
from kai.agent.goal import GoalManager
from kai.agent.tools.brain import register_brain_tool
from kai.bots import list_bots, load_bot
from kai.brain.client import MorphikClient
from kai.brain.config import get_brain_settings
from kai.cli.style import (
    ACCENT,
    DIM,
    ERR,
    GL_ARROW,
    GL_ERR,
    GL_IDLE,
    GL_OK,
    GL_RUN,
    OK,
    WARN,
    BotStartupError,
    _relative,
    _uptime_seconds,
    card,
    console,
    err_line,
    soft_table,
)
from kai.config.settings import Settings, get_settings
from kai.logging.logger import setup_logging
from kai.runs import RunRecord, RunRegistry, generate_run_id, runs_path
from kai.utils.common import compute_hmac, now_iso

logger = logging.getLogger(__name__)


def _runs_registry(bot_name: str, settings: Settings) -> RunRegistry:
    return RunRegistry(runs_path(settings.agent_history_folder, bot_name))


def _instance_id(bot_name: str, user: str = "") -> str:
    """Per-user instance id matching what `start` registers under."""
    return f"{bot_name}-{user}" if user else bot_name


def _parse_tool_list(raw: str) -> list[str]:
    """Parse a comma-separated ``--enable-tools`` / ``--disable-tools``
    value. Empty / whitespace-only -> ``[]``.
    """
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _fail_on_errors(errors: list[str]) -> bool:
    """Print errors and return True if any were found."""
    if not errors:
        return False
    for err in errors:
        err_line(err)
    return True


def _fail_on_tool_resolution(tool_resolution) -> bool:
    """Print tool-resolution errors and return True if any were found."""
    had_errors = False
    for err in tool_resolution.missing_required:
        err_line(f"required tool missing: {err}")
        had_errors = True
    for err in tool_resolution.rejected_disable:
        err_line(f"cannot disable: {err}")
        had_errors = True
    for name in tool_resolution.rejected_unknown:
        err_line(f"unknown tool in --enable-tools: {name}")
        had_errors = True
    return had_errors


def _resolve_run(bot_name: str, run_id: str, user: str = "") -> RunRecord:
    """Resolve a run_id to a live RunRecord or exit with an error panel."""
    settings = get_settings()
    setup_logging()
    instance_id = _instance_id(bot_name, user)
    registry = _runs_registry(instance_id, settings)
    record = registry.active().get(run_id)
    if record is None:
        hint = f"kai runs {bot_name}" + (f" --user {user}" if user else "")
        err_line(f"unknown or stale run: {run_id}", hint=f"check `{hint}` for active run_ids")
        raise typer.Exit(1)
    return record


def _post_tell(record: RunRecord, message: str, *, persist: bool) -> tuple[int, dict]:
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


def _post_clear(record: RunRecord) -> tuple[int, dict]:
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


def _render_status(data: dict, *, uptime: int | None = None) -> None:
    """Render a status snapshot dict (from the ``/status`` route) as bullets."""
    lines: list = []

    if uptime is not None:
        lines.append(_format_uptime_line(uptime))

    lines.extend(_format_tasks_lines(data.get("tasks")))
    lines.extend(_format_caps_lines(data.get("capabilities")))

    from rich.console import Group

    console.print(Group(*(Align.left(line) for line in lines)))


def _format_uptime_line(uptime: int) -> str:
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


def _format_tasks_lines(tasks: dict | None) -> list[str]:
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


def _format_caps_lines(caps: dict | None) -> list[str]:
    if not caps:
        return []
    flags = []
    for key, label in [("vision", "vision")]:
        flags.append(f"[{OK}]{label}[/{OK}]" if caps.get(key) else f"[{DIM}]{label}[/{DIM}]")
    return ["[bold]capabilities[/bold]  " + "  ".join(flags)]


def _render_tell(data: dict) -> None:
    """Render a TellResult dict as a compact, styled summary."""
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
        table.add_row(*_render_tell_row(a))
    console.print(table)


def _render_tell_row(a: dict) -> tuple:
    name = a.get("tool", "?")
    a_ok = a.get("ok", False)
    mark = GL_OK if a_ok else GL_ERR
    target = _first_nonempty(a, ("chat_id", "target"))
    target = _truncate(target, 40) if target else f"[{DIM}]\u2014[/{DIM}]"
    text = _first_nonempty(a, ("text", "goal")) or _first_other_arg(a)
    text = _truncate(text, 80) if text else f"[{DIM}]\u2014[/{DIM}]"
    status = f"[{OK}]ok[/{OK}]" if a_ok else f"[{ERR}]failed[/{ERR}]"
    return mark, name, target, text, status


def _first_nonempty(a: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        val = a.get(key)
        if val:
            return str(val)
    return ""


def _first_other_arg(a: dict) -> str:
    for key, val in a.items():
        if key in ("tool", "ok", "target", "chat_id"):
            continue
        if val is None or val == "" or val is False:
            continue
        return str(val)
    return ""


def _truncate(text: str, limit: int) -> str:
    return text[: limit - 3] + "..." if len(text) > limit else text


def _render_help() -> None:
    """Render the slash-command list shown by ``/help``."""
    table = soft_table(("command", "magenta"), ("description", DIM))
    table.add_row("/help", "list these commands")
    table.add_row("/quit /exit", "leave the chat")
    table.add_row("/persist", "toggle persistent changes (set_goal)")
    table.add_row("/clear", "reset the operator conversation history")
    console.print(table)


def _start(
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
    # and reproduces today's behavior. Boot guards fail fast on templates
    # declaring required tools without configured env vars, transport-invalid
    # actions, disabled default/required tools, or typos in --enable-tools.
    from kai.templates import TemplateRegistry
    from kai.templates.resolver import resolve_tools, validate_actions

    transport = bot_name
    tmpl_name = template_name or "general"

    registry = TemplateRegistry.bundled()
    try:
        tmpl = registry.get(transport, tmpl_name)
    except FileNotFoundError:
        err_line(f"template not found: {transport}/{tmpl_name}")
        raise typer.Exit(1)

    if _fail_on_errors(validate_actions(tmpl)):
        raise typer.Exit(1)

    operator_enable = _parse_tool_list(enable_tools)
    operator_disable = _parse_tool_list(disable_tools)

    tool_resolution = resolve_tools(tmpl, operator_enable, operator_disable)
    if _fail_on_tool_resolution(tool_resolution):
        raise typer.Exit(1)

    # Instance namespace: when --user is provided, isolate files per user.
    instance_id = _instance_id(bot_name, user)

    if user:
        bot.instance = instance_id

    agent = KaiAgent(settings=settings, goal_manager=GoalManager(), namespace=instance_id)
    if goal_text:
        agent.goal_manager.set_goal(goal_text)

    console.print(
        f"[bold magenta]kai[/bold magenta] [{DIM}]v0.0.1[/{DIM}]  {GL_ARROW} "
        f"[{DIM}]starting[/{DIM}] [bold]{bot.name}[/bold]  "
        f"[{DIM}]{transport}/{tmpl_name}[/{DIM}]"
    )

    async def _main() -> int:
        stop_task: asyncio.Task | None = None
        run_id: str | None = None
        brain_client: MorphikClient | None = None
        sql_engine = None

        try:
            bot.configure(agent, settings, template=tmpl, tools=tool_resolution)
        except (FileNotFoundError, ValueError, OSError) as exc:
            err_line(f"configuration error  {exc}")
            return 1

        brain_settings = get_brain_settings()
        if brain_settings.brain_enabled and "brain_query" in tool_resolution.final_tools:
            try:
                brain_client = MorphikClient(brain_settings)
                register_brain_tool(
                    agent,
                    brain_client,
                    workspace=brain_settings.workspace,
                    instruction=brain_settings.instruction,
                    mandatory=brain_settings.mandatory,
                )
                logger.info(
                    "brain_query tool registered (end_user_id=%s)", brain_settings.workspace
                )
                if brain_settings.mandatory:
                    # Mandatory Brain: the workflow prompt (built with
                    # mandatory=True) instructs the model to call brain_query
                    # first, fall back to web_search when the Brain has nothing,
                    # and never answer facts from memory. Lowering temperature
                    # (greedy decoding) steers the model toward following that
                    # MUST instruction. This is strong steering, not a
                    # code-level guarantee.
                    agent.set_temperature(brain_settings.mandatory_temperature)
                    logger.info(
                        "brain mandatory: MUST-use prompt + web fallback; "
                        "LLM temperature set to %s",
                        brain_settings.mandatory_temperature,
                    )
            except Exception:
                logger.exception("failed to register brain_query tool; continuing without it")
                brain_client = None
        else:
            for warning in brain_settings.validate_startup():
                logger.debug("brain disabled: %s", warning)

        from kai.agent.tools.sql import get_sql_settings

        sql_settings = get_sql_settings()
        if sql_settings.sql_enabled and "sql_query" in tool_resolution.final_tools:
            try:
                from kai.agent.tools.sql import register_sql_tool

                sql_engine = register_sql_tool(
                    agent,
                    sql_settings.dsn,
                    instruction=sql_settings.instruction,
                    row_limit=sql_settings.row_limit,
                )
                logger.info("sql_query tool registered")
            except Exception:
                logger.exception("failed to register sql_query tool; continuing without it")

        from kai.agent.tools.email import get_smtp_settings

        smtp_settings = get_smtp_settings()
        if smtp_settings.smtp_enabled and "send_email" in tool_resolution.final_tools:
            try:
                from kai.agent.tools.email import register_email_tool

                register_email_tool(
                    agent,
                    host=smtp_settings.host,
                    port=smtp_settings.port,
                    username=smtp_settings.username,
                    password=smtp_settings.password,
                    from_address=smtp_settings.from_address,
                    use_tls=smtp_settings.use_tls,
                    instruction=smtp_settings.instruction,
                    display_name=bot.display_name(),
                )
                logger.info("send_email tool registered")
            except Exception:
                logger.exception("failed to register send_email tool; continuing without it")

        from kai.agent.tools.calcom import get_calcom_settings

        calcom_settings = get_calcom_settings()
        if calcom_settings.calcom_enabled and "calcom" in tool_resolution.final_tools:
            try:
                from kai.agent.tools.calcom import register_calcom_tool

                register_calcom_tool(
                    agent,
                    api_key=calcom_settings.api_key,
                    base_url=calcom_settings.base_url,
                    instruction=calcom_settings.instruction,
                )
                logger.info("calcom tools registered")
            except Exception:
                logger.exception("failed to register calcom tools; continuing without it")

        # Register a run_id so `kai tell` can target this instance.
        # Bots that opt out of tell return None from tell_endpoint().
        endpoint = bot.tell_endpoint()
        if endpoint is not None:
            run_id = generate_run_id()
            registry = _runs_registry(instance_id, settings)
            registry.replace(
                run_id,
                RunRecord(
                    endpoint=endpoint,
                    hmac_key=bot.tell_hmac_key() or "",
                    hmac_algorithm=bot.tell_hmac_algorithm(),
                    pid=os.getpid(),
                    started_at=now_iso(),
                ),
            )
            print(f"KAI_RUN_ID={run_id}", flush=True)
            card(
                "[bold]run id[/bold]",
                f"[bold yellow]{run_id}[/bold yellow]\n"
                f'[{DIM}]use:[/{DIM}] kai tell {instance_id} --run {run_id} -m "..."',
                border=WARN,
            )

        loop = asyncio.get_running_loop()
        shutdown_requested = asyncio.Event()
        force_quit = asyncio.Event()
        run_task = asyncio.ensure_future(bot.run())

        def _request_shutdown():
            if not shutdown_requested.is_set():
                shutdown_requested.set()
                nonlocal stop_task
                stop_task = asyncio.ensure_future(bot.stop())
                stop_task.add_done_callback(_on_stop_done)
            elif not force_quit.is_set():
                force_quit.set()
                err_line("forcing exit")
                run_task.cancel()

        def _on_stop_done(task: asyncio.Task):
            if task.exception() and not task.cancelled():
                logger.error("bot.stop() raised: %s", task.exception())

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _request_shutdown)

        exit_code = 0
        try:
            await run_task
        except asyncio.CancelledError:
            err_line("forced shutdown")
        except BotStartupError as exc:
            err_line(f"startup failed  {exc}")
            exit_code = 1
        finally:
            if stop_task and not stop_task.done():
                try:
                    await asyncio.wait_for(stop_task, timeout=5.0)
                except TimeoutError:
                    logger.debug("bot.stop() did not finish within 5s; proceeding")
                except Exception:
                    logger.debug("bot.stop() raised during shutdown", exc_info=True)
            await agent.flush()
            await agent.aclose()
            if brain_client is not None:
                try:
                    await brain_client.close()
                except Exception:
                    logger.debug("brain_client.close() raised during shutdown", exc_info=True)
            if sql_engine is not None:
                try:
                    sql_engine.dispose()
                except Exception:
                    logger.debug("sql_engine.dispose() raised during shutdown", exc_info=True)
            if run_id is not None:
                try:
                    _runs_registry(instance_id, settings).remove(run_id)
                except Exception:
                    logger.debug("failed to unregister run %s", run_id, exc_info=True)
            console.print(f"{GL_IDLE} [{DIM}]kai stopped[/{DIM}]")
        return exit_code

    exit_code = asyncio.run(_main())
    if exit_code:
        raise typer.Exit(exit_code)


def _list_cmd() -> None:
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


def _status(
    bot_name: str,
    run_id: str,
    user: str,
) -> None:
    """Show a running bot's transport/session status.

    Targets a specific run (from `kai start` output), not a bot name: it GETs
    the run's ``/status`` route and renders the snapshot the bot returns.
    """
    record = _resolve_run(bot_name, run_id, user=user)

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

    _render_status(data, uptime=_uptime_seconds(record.started_at))


def _tell(
    bot_name: str,
    run_id: str,
    message: str,
    persist: bool,
    user: str,
) -> None:
    """Send an instruction to a running bot via its /tell route.

    Targets a specific run (from `kai start` output), not a bot name. The
    CLI is bot-agnostic: it forwards ``{message, persist}`` verbatim and
    prints the structured ``TellResult`` the bot returns.
    """
    record = _resolve_run(bot_name, run_id, user=user)

    try:
        status_code, data = _post_tell(record, message, persist=persist)
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

    _render_tell(data)
    if status_code != 200 or not data.get("ok"):
        raise typer.Exit(1)


def _chat(
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
    record = _resolve_run(bot_name, run_id, user=user)

    console.print(
        f"[bold magenta]kai chat[/bold magenta] [{DIM}]{bot_name} \u00b7 {run_id}[/{DIM}]"
    )
    console.print(f"[{DIM}]/help for commands \u00b7 Ctrl+D to quit[/{DIM}]")
    console.rule(style="dim")

    local_persist = persist

    while True:
        tag = _persist_tag(local_persist)
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
            local_persist = _handle_slash(message, local_persist, record)
            continue

        data = _post_and_clean_tell(record, message, local_persist)
        if data is not None:
            _render_tell(data)


def _post_and_clean_tell(record, message: str, persist: bool) -> dict | None:
    """POST a /tell and return cleaned data, or None on failure."""
    try:
        with console.status(f"[{DIM}]thinking...[/{DIM}]", spinner="dots"):
            _status_code, data = _post_tell(record, message, persist=persist)
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


def _persist_tag(local_persist: bool) -> str:
    if local_persist:
        return "[bold green]\u25cfpersist[/bold green]"
    return f"[{DIM}]\u25cb[/{DIM}]"


def _handle_slash(message: str, local_persist: bool, record) -> bool:
    """Handle a slash command (except /quit, handled by the caller).
    Returns the (possibly toggled) ``local_persist`` value."""
    if message == "/help":
        _render_help()
        return local_persist
    if message == "/persist":
        local_persist = not local_persist
        state = "on" if local_persist else "off"
        console.print(f"[{DIM}]persist[/{DIM}] [bold]{state}[/bold]")
        return local_persist
    if message == "/clear":
        _do_clear(record)
    return local_persist


def _do_clear(record) -> None:
    try:
        status_code, data = _post_clear(record)
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


def _stop(
    bot_name: str,
    run_id: str,
    force: bool,
    user: str,
) -> None:
    """Stop a running bot instance.

    Resolves a ``run_id`` (from `kai start`) to its process and sends it a
    signal: SIGTERM by default, which the bot's signal handler turns into a
    graceful shutdown (flushes history, unregisters the run). ``--force``
    sends SIGKILL — use only if the bot is wedged, as its run record is
    pruned lazily on the next `kai runs` rather than cleaned up by the bot.
    """
    instance_id = _instance_id(bot_name, user)
    record = _resolve_run(bot_name, run_id, user=user)

    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(record.pid, sig)
    except ProcessLookupError:
        # Already gone — prune the stale record so `kai runs` is accurate.
        _runs_registry(instance_id, get_settings()).remove(run_id)
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


def _runs_cmd(
    bot_name: str,
    user: str,
) -> None:
    """List active runs for a bot (recovers a forgotten run_id)."""
    settings = get_settings()
    instance_id = _instance_id(bot_name, user)
    registry = _runs_registry(instance_id, settings)
    active = registry.active()
    if not active:
        console.print(f"[{DIM}]{instance_id}[/{DIM}]  [{DIM}]no active runs[/{DIM}]")
        return
    console.print(f"[bold]{instance_id}[/bold]  [{DIM}]active runs[/{DIM}]")
    table = soft_table(
        ("run id", "bold yellow"),
        ("endpoint", ACCENT),
        ("pid", DIM),
        ("started", DIM),
    )
    for rid, record in active.items():
        table.add_row(rid, record.endpoint, str(record.pid), _relative(record.started_at))
    console.print(table)


def register(app: typer.Typer) -> None:
    """Register all bot lifecycle commands on the given typer app."""

    @app.command()
    def start(
        bot_name: str = typer.Argument(..., help="Bot to start (e.g. 'email')"),
        goal_text: str = typer.Option("", "--goal", "-g", help="Runtime goal"),
        language: str = typer.Option("", "--language", "-l", help="Override bot language"),
        user: str = typer.Option("", "--user", "-u", help="User email (per-instance namespace)"),
        template: str = typer.Option(
            "general",
            "--template",
            "-t",
            help="Template to use (default: general)",
        ),
        enable_tools: str = typer.Option(
            "",
            "--enable-tools",
            help="Comma-separated tools to force-enable beyond the template",
        ),
        disable_tools: str = typer.Option(
            "",
            "--disable-tools",
            help="Comma-separated tools to disable from the template",
        ),
    ):
        _start(
            bot_name,
            goal_text,
            language,
            user,
            template,
            enable_tools,
            disable_tools,
        )

    @app.command(name="list")
    def list_cmd():
        _list_cmd()

    @app.command()
    def status(
        bot_name: str = typer.Argument(..., help="Bot to query (e.g. 'email')"),
        run_id: str = typer.Option(..., "--run", help="run_id of the target `kai start` instance"),
        user: str = typer.Option(
            "",
            "--user",
            "-u",
            help="User email the instance was started with (--user on `kai start`)",
        ),
    ):
        _status(bot_name, run_id, user)

    @app.command()
    def tell(
        bot_name: str = typer.Argument(..., help="Bot to instruct (e.g. 'email')"),
        run_id: str = typer.Option(..., "--run", help="run_id of the target `kai start` instance"),
        message: str = typer.Option(..., "--message", "-m", help="Instruction text for the bot"),
        persist: bool = typer.Option(
            False, "--persist", help="Allow permanent changes (e.g. set_goal)"
        ),
        user: str = typer.Option(
            "",
            "--user",
            "-u",
            help="User email the instance was started with (--user on `kai start`)",
        ),
    ):
        _tell(bot_name, run_id, message, persist, user)

    @app.command()
    def chat(
        bot_name: str = typer.Argument(..., help="Bot to chat with (e.g. 'email')"),
        run_id: str = typer.Option(..., "--run", help="run_id of the target `kai start` instance"),
        persist: bool = typer.Option(False, "--persist", help="Allow permanent changes"),
        user: str = typer.Option(
            "",
            "--user",
            "-u",
            help="User email the instance was started with (--user on `kai start`)",
        ),
    ):
        _chat(bot_name, run_id, persist, user)

    @app.command()
    def stop(
        bot_name: str = typer.Argument(..., help="Bot to stop (e.g. 'email')"),
        run_id: str = typer.Option(..., "--run", help="run_id of the target `kai start` instance"),
        force: bool = typer.Option(
            False, "--force", help="Send SIGKILL instead of SIGTERM (no graceful shutdown)"
        ),
        user: str = typer.Option(
            "",
            "--user",
            "-u",
            help="User email the instance was started with (--user on `kai start`)",
        ),
    ):
        _stop(bot_name, run_id, force, user)

    @app.command(name="runs")
    def runs_cmd(
        bot_name: str = typer.Argument(..., help="Bot whose runs to list (e.g. 'email')"),
        user: str = typer.Option(
            "",
            "--user",
            "-u",
            help="User email the instance was started with (--user on `kai start`)",
        ),
    ):
        _runs_cmd(bot_name, user)
