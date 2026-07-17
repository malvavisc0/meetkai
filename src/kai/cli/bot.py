import asyncio
import base64
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
from kai.brain.client import LightRagClient
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
from kai.utils.terminal import render_image_pixelated

logger = logging.getLogger(__name__)


def _runs_registry(bot_name: str, settings: Settings) -> RunRegistry:
    return RunRegistry(runs_path(settings.agent_history_folder, bot_name))


def _instance_id(bot_name: str, user: str = "") -> str:
    """Mirror `start`'s per-user namespacing so lookups hit the same runs file.

    `kai start <bot> --user <email>` registers its run under
    `<bot>-<email>.runs.json` (see `start()` above). Any command that later
    needs to resolve a run_id for that instance must derive the same
    instance id from `--user`, or it will silently look at the wrong
    (bot-only) runs file.
    """
    return f"{bot_name}-{user}" if user else bot_name


def _parse_tool_list(raw: str) -> list[str]:
    """Parse a comma-separated ``--enable-tools`` / ``--disable-tools`` value.

    Empty / whitespace-only → ``[]``. Entries are stripped and empties dropped.
    No name validation here — ``resolve_tools`` records typos as
    ``rejected_unknown`` and boot fails on them.
    """
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


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


def _post_sleep_toggle(record: RunRecord, chat_id: str, *, action: str) -> tuple[int, dict]:
    """POST an HMAC-signed ``{"chat_id": ...}`` to the run's /sleep or /wake route."""
    body = json.dumps({"chat_id": chat_id}).encode("utf-8")
    signature = compute_hmac(record.hmac_key, body, record.hmac_algorithm)
    resp = httpx.post(
        f"{record.endpoint}/{action}",
        content=body,
        headers={"Content-Type": "application/json", "X-Webhook-Hmac": signature},
        timeout=30.0,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"ok": False, "reply": resp.text}
    return resp.status_code, data


def _render_status(data: dict, *, uptime: int | None = None) -> None:
    """Render a status snapshot dict (from the ``/status`` route) as bullets."""
    session = data.get("session")
    lines: list = []

    if uptime is not None:
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
        lines.append(f"[blue]\u25cf[/blue] [bold]uptime[/bold]  [{DIM}]{dur}  ({uptime}s)[/{DIM}]")

    if session:
        sname = session.get("name", "unknown")
        sstatus = session.get("status", "unknown")
        color = OK if sstatus == "WORKING" else WARN
        lines.append(
            f"[{color}]\u25cf[/{color}] [bold]session[/bold]  {sname}  [{color}]{sstatus}[/{color}]"
        )
    else:
        lines.append(f"[{WARN}]\u25cf[/{WARN}] [bold]session[/bold]  [{DIM}]not found[/{DIM}]")

    account = data.get("account")
    if account:
        picture_b64 = account.get("picture")
        if picture_b64:
            try:
                render_image_pixelated(base64.b64decode(picture_b64), console, width=16)
            except Exception:
                logger.debug("failed to render status profile picture")
        if account.get("name"):
            lines.append(f"   [cyan]{account['name']}[/cyan]")
        if account.get("id"):
            lines.append(f"   [{DIM}]{account['id']}[/{DIM}]")

    sleep = data.get("sleep")
    if sleep:
        sleeping = sleep.get("sleeping", [])
        count = sleep.get("count", len(sleeping))
        if count:
            color = "magenta"
            lines.append(
                f"[{color}]\u25cf[/{color}] [bold]sleep[/bold]  "
                f"[{DIM}]{count} chat(s) asleep[/{DIM}]"
            )
            for chat_id in sleeping[:5]:
                lines.append(f"   [{DIM}]{chat_id}[/{DIM}]")
            if count > 5:
                lines.append(f"   [{DIM}]\u2026 +{count - 5} more[/{DIM}]")
        else:
            lines.append(f"[{OK}]\u25cf[/{OK}] [bold]sleep[/bold]  [{DIM}]awake[/{DIM}]")

    tasks = data.get("tasks")
    if tasks:
        pending = tasks.get("pending", 0)
        recurring = tasks.get("recurring", 0)
        total = pending + recurring
        if total:
            parts = []
            if pending:
                parts.append(f"{pending} pending")
            if recurring:
                parts.append(f"{recurring} recurring")
            lines.append(
                f"[cyan]\u25cf[/cyan] [bold]tasks[/bold]  [{DIM}]{', '.join(parts)}[/{DIM}]"
            )
            for item in tasks.get("items", [])[:5]:
                repeat = item.get("repeat", "none")
                tag = f" ({repeat})" if repeat != "none" else ""
                lines.append(f"   [{DIM}]{item.get('goal', '?')[:60]}{tag}[/{DIM}]")
            if total > 5:
                lines.append(f"   [{DIM}]\u2026 +{total - 5} more[/{DIM}]")
        else:
            lines.append(f"[{OK}]\u25cf[/{OK}] [bold]tasks[/bold]  [{DIM}]none[/{DIM}]")

    caps = data.get("capabilities")
    if caps:
        flags = []
        flag_map = [
            ("voice_to_text", "voice to text"),
            ("text_to_voice", "text to voice"),
            ("vision", "vision"),
            ("instagram", "instagram"),
        ]
        for key, label in flag_map:
            on = caps.get(key)
            if on:
                flags.append(f"[{OK}]{label}[/{OK}]")
            else:
                flags.append(f"[{DIM}]{label}[/{DIM}]")
        lines.append("[bold]capabilities[/bold]  " + "  ".join(flags))

    from rich.console import Group

    console.print(Group(*(Align.left(line) for line in lines)))


def _render_tell(data: dict) -> None:
    """Render a TellResult dict as a compact, styled summary."""
    ok = data.get("ok", False)
    reply = data.get("reply", "") or "(no reply)"

    mark = GL_OK if ok else GL_ERR
    label = f"[{OK}]reply[/{OK}]" if ok else f"[{ERR}]error[/{ERR}]"
    console.print(f"{mark} {label}")
    console.print(f"  {reply}")

    actions = data.get("actions") or []
    if actions:
        table = soft_table(
            ("", ""),
            ("tool", ACCENT),
            ("target", DIM),
            ("text", ""),
            ("status", ""),
        )
        for a in actions:
            name = a.get("tool", "?")
            a_ok = a.get("ok", False)
            mark = GL_OK if a_ok else GL_ERR

            # target: chat_id / target (where it was delivered)
            target = ""
            for key in ("chat_id", "target"):
                val = a.get(key)
                if val:
                    target = str(val)
                    break
            if not target:
                target = f"[{DIM}]\u2014[/{DIM}]"
            elif len(target) > 40:
                target = target[:37] + "..."

            # text: the message content / goal / any other arg value
            text = ""
            for key in ("text", "goal"):
                val = a.get(key)
                if val:
                    text = str(val)
                    break
            if not text:
                for key, val in a.items():
                    if key in ("tool", "ok", "target", "chat_id"):
                        continue
                    if val is None or val == "" or val is False:
                        continue
                    text = str(val)
                    break
            if not text:
                text = f"[{DIM}]\u2014[/{DIM}]"
            elif len(text) > 80:
                text = text[:77] + "..."

            status = f"[{OK}]ok[/{OK}]" if a_ok else f"[{ERR}]failed[/{ERR}]"
            table.add_row(mark, name, target, text, status)
        console.print(table)


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
    voice: str,
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
    # and reproduces today's behavior. The boot guards below fail fast on a
    # template declaring a required tool whose env vars are not configured,
    # on a template declaring transport-invalid actions, on an operator trying
    # to disable a default/required tool, and on an operator typo in
    # --enable-tools (phantom-enable validation).
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

    action_errors = validate_actions(tmpl)
    if action_errors:
        for err in action_errors:
            err_line(err)
        raise typer.Exit(1)

    operator_enable = _parse_tool_list(enable_tools)
    operator_disable = _parse_tool_list(disable_tools)

    tool_resolution = resolve_tools(tmpl, operator_enable, operator_disable)
    if tool_resolution.missing_required:
        for err in tool_resolution.missing_required:
            err_line(f"required tool missing: {err}")
        raise typer.Exit(1)
    if tool_resolution.rejected_disable:
        for err in tool_resolution.rejected_disable:
            err_line(f"cannot disable: {err}")
        raise typer.Exit(1)
    if tool_resolution.rejected_unknown:
        for name in tool_resolution.rejected_unknown:
            err_line(f"unknown tool in --enable-tools: {name}")
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
        brain_client: LightRagClient | None = None
        sql_engine = None

        try:
            bot.configure(
                agent, settings, voice=voice or None, template=tmpl, tools=tool_resolution
            )
        except (FileNotFoundError, ValueError, OSError) as exc:
            err_line(f"configuration error  {exc}")
            return 1

        brain_settings = get_brain_settings()
        if brain_settings.brain_enabled and "brain_query" in tool_resolution.final_tools:
            try:
                brain_client = LightRagClient(brain_settings)
                register_brain_tool(
                    agent,
                    brain_client,
                    workspace=brain_settings.workspace,
                    instruction=brain_settings.instruction,
                    mandatory=brain_settings.mandatory,
                )
                logger.info("brain_query tool registered (workspace=%s)", brain_settings.workspace)
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
        if local_persist:
            tag = "[bold green]\u25cfpersist[/bold green]"
        else:
            tag = f"[{DIM}]\u25cb[/{DIM}]"
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
        if message == "/help":
            _render_help()
            continue
        if message == "/persist":
            local_persist = not local_persist
            state = "on" if local_persist else "off"
            console.print(f"[{DIM}]persist[/{DIM}] [bold]{state}[/bold]")
            continue
        if message == "/clear":
            try:
                status_code, data = _post_clear(record)
            except httpx.HTTPError as exc:
                err_line(f"failed to reach {record.endpoint}  {exc}")
                continue
            ok = status_code == 200 and isinstance(data, dict) and data.get("ok")
            if ok:
                console.print(f"{GL_OK} [{OK}]clear[/{OK}]  [{DIM}]history cleared[/{DIM}]")
            elif isinstance(data, dict):
                err_line(data.get("error", "clear failed"))
            else:
                err_line("clear failed")
            continue

        try:
            with console.status(f"[{DIM}]thinking...[/{DIM}]", spinner="dots"):
                status_code, data = _post_tell(record, message, persist=local_persist)
        except httpx.HTTPError as exc:
            err_line(f"failed to reach {record.endpoint}  {exc}")
            continue

        if isinstance(data, dict):
            data = {
                k: v
                for k, v in data.items()
                if not (v is None or (isinstance(v, (list, str)) and not v))
            }
            data.setdefault("ok", False)

        _render_tell(data)


def _sleep(
    bot_name: str,
    run_id: str,
    chat_id: str,
    user: str,
) -> None:
    """Put a chat to sleep on a running bot via its /sleep route.

    A sleeping bot stops speaking in that chat but keeps observing messages.
    """
    record = _resolve_run(bot_name, run_id, user=user)
    try:
        status_code, data = _post_sleep_toggle(record, chat_id, action="sleep")
    except httpx.HTTPError as exc:
        err_line(f"failed to reach {record.endpoint}  {exc}")
        raise typer.Exit(1) from exc

    if status_code == 200 and data.get("ok"):
        console.print(
            f"{GL_OK} [{OK}]asleep[/{OK}]  [bold]{chat_id}[/bold]  [{DIM}]now asleep[/{DIM}]"
        )
    else:
        err_line(data.get("error", data.get("reply", "failed")))
    if status_code != 200 or not data.get("ok"):
        raise typer.Exit(1)


def _wake(
    bot_name: str,
    run_id: str,
    chat_id: str,
    user: str,
) -> None:
    """Wake a chat up on a running bot via its /wake route."""
    record = _resolve_run(bot_name, run_id, user=user)
    try:
        status_code, data = _post_sleep_toggle(record, chat_id, action="wake")
    except httpx.HTTPError as exc:
        err_line(f"failed to reach {record.endpoint}  {exc}")
        raise typer.Exit(1) from exc

    if status_code == 200 and data.get("ok"):
        console.print(
            f"{GL_OK} [{OK}]awake[/{OK}]  [bold]{chat_id}[/bold]  [{DIM}]now awake[/{DIM}]"
        )
    else:
        err_line(data.get("error", data.get("reply", "failed")))
    if status_code != 200 or not data.get("ok"):
        raise typer.Exit(1)


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
        bot_name: str = typer.Argument(..., help="Bot to start (e.g. 'waha')"),
        goal_text: str = typer.Option("", "--goal", "-g", help="Runtime goal"),
        language: str = typer.Option("", "--language", "-l", help="Override bot language"),
        user: str = typer.Option("", "--user", "-u", help="User email (per-instance namespace)"),
        voice: str = typer.Option("", "--voice", "-v", help="Override kokoro voice"),
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
            voice,
            template,
            enable_tools,
            disable_tools,
        )

    @app.command(name="list")
    def list_cmd():
        _list_cmd()

    @app.command()
    def status(
        bot_name: str = typer.Argument(..., help="Bot to query (e.g. 'waha')"),
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
        bot_name: str = typer.Argument(..., help="Bot to instruct (e.g. 'waha')"),
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
        bot_name: str = typer.Argument(..., help="Bot to chat with (e.g. 'waha')"),
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
    def sleep(
        bot_name: str = typer.Argument(..., help="Bot to target (e.g. 'waha')"),
        run_id: str = typer.Option(..., "--run", help="run_id of the target `kai start` instance"),
        chat_id: str = typer.Option(..., "--chat", help="Chat ID to put to sleep (e.g. a JID)"),
        user: str = typer.Option(
            "",
            "--user",
            "-u",
            help="User email the instance was started with (--user on `kai start`)",
        ),
    ):
        _sleep(bot_name, run_id, chat_id, user)

    @app.command()
    def wake(
        bot_name: str = typer.Argument(..., help="Bot to target (e.g. 'waha')"),
        run_id: str = typer.Option(..., "--run", help="run_id of the target `kai start` instance"),
        chat_id: str = typer.Option(..., "--chat", help="Chat ID to wake up (e.g. a JID)"),
        user: str = typer.Option(
            "",
            "--user",
            "-u",
            help="User email the instance was started with (--user on `kai start`)",
        ),
    ):
        _wake(bot_name, run_id, chat_id, user)

    @app.command()
    def stop(
        bot_name: str = typer.Argument(..., help="Bot to stop (e.g. 'waha')"),
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
        bot_name: str = typer.Argument(..., help="Bot whose runs to list (e.g. 'waha')"),
        user: str = typer.Option(
            "",
            "--user",
            "-u",
            help="User email the instance was started with (--user on `kai start`)",
        ),
    ):
        _runs_cmd(bot_name, user)
