from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import typer

from kai.agent.core import KaiAgent
from kai.agent.tools.brain import register_brain_tool
from kai.bots.base import BaseBot
from kai.brain.client import MorphikClient
from kai.brain.config import get_brain_settings
from kai.cli.style import (
    DIM,
    GL_IDLE,
    WARN,
    BotStartupError,
    card,
    console,
    err_line,
)
from kai.config.settings import Settings
from kai.runs import (
    RunRecord,
    RunRegistry,
    generate_run_id,
    proc_start_time,
)
from kai.templates.resolver import ToolResolution
from kai.templates.schema import TemplateDef
from kai.utils.common import now_iso

if TYPE_CHECKING:
    from sqlalchemy import Engine

logger = logging.getLogger(__name__)


def parse_tool_list(raw: str) -> list[str]:
    """Parse a comma-separated ``--enable-tools`` / ``--disable-tools``
    value. Empty / whitespace-only -> ``[]``.
    """
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def fail_on_errors(errors: list[str]) -> bool:
    """Print errors and return True if any were found."""
    if not errors:
        return False
    for err in errors:
        err_line(err)
    return True


def fail_on_tool_resolution(tool_resolution: ToolResolution) -> bool:
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


def load_startup_template(
    bot_name: str,
    template_name: str,
    enable_tools: str,
    disable_tools: str,
) -> tuple[TemplateDef, ToolResolution]:
    """Resolve the start template and its final tool set, or exit on error.

    Boot guards fail fast on templates declaring required tools without
    configured env vars, transport-invalid actions, disabled default/required
    tools, or typos in ``--enable-tools``.
    """
    from kai.templates import TemplateRegistry
    from kai.templates.resolver import resolve_tools, validate_actions

    transport = bot_name
    tmpl_name = template_name or "general"
    registry = TemplateRegistry.bundled()
    try:
        tmpl = registry.get(transport, tmpl_name)
    except FileNotFoundError:
        err_line(f"template not found: {transport}/{tmpl_name}")
        raise typer.Exit(1) from None
    except ValueError as exc:
        err_line(f"invalid template {transport}/{tmpl_name}: {exc}")
        raise typer.Exit(1) from exc

    if fail_on_errors(validate_actions(tmpl)):
        raise typer.Exit(1)

    tool_resolution = resolve_tools(
        tmpl, parse_tool_list(enable_tools), parse_tool_list(disable_tools)
    )
    if fail_on_tool_resolution(tool_resolution):
        raise typer.Exit(1)
    return tmpl, tool_resolution


def _register_brain_tool(agent: KaiAgent, tool_resolution: ToolResolution) -> MorphikClient | None:
    """Register ``brain_query`` when enabled and requested, or return None."""
    brain_settings = get_brain_settings()
    if not (brain_settings.brain_enabled and "brain_query" in tool_resolution.final_tools):
        for warning in brain_settings.validate_startup():
            logger.debug("brain disabled: %s", warning)
        return None
    try:
        brain_client = MorphikClient(brain_settings)
        register_brain_tool(
            agent,
            brain_client,
            workspace=brain_settings.workspace,
            instruction=brain_settings.instruction,
            mandatory=brain_settings.mandatory,
        )
        logger.info("brain_query tool registered (end_user_id=%s)", brain_settings.workspace)
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
                "brain mandatory: MUST-use prompt + web fallback; LLM temperature set to %s",
                brain_settings.mandatory_temperature,
            )
        return brain_client
    except Exception:
        logger.exception("failed to register brain_query tool; continuing without it")
        return None


def _register_sql_tool(agent: KaiAgent, tool_resolution: ToolResolution) -> Engine | None:
    """Register ``sql_query`` when enabled and requested, or return None."""
    from kai.agent.tools.sql import get_sql_settings

    sql_settings = get_sql_settings()
    if not (sql_settings.sql_enabled and "sql_query" in tool_resolution.final_tools):
        return None
    try:
        from kai.agent.tools.sql import register_sql_tool

        sql_engine = register_sql_tool(
            agent,
            sql_settings.dsn,
            instruction=sql_settings.instruction,
            row_limit=sql_settings.row_limit,
        )
        logger.info("sql_query tool registered")
        return sql_engine
    except Exception:
        logger.exception("failed to register sql_query tool; continuing without it")
        return None


def _register_email_tool(agent: KaiAgent, tool_resolution: ToolResolution, bot: BaseBot) -> None:
    """Register ``send_email`` when enabled and requested."""
    from kai.agent.tools.email import get_smtp_settings

    smtp_settings = get_smtp_settings()
    if not (smtp_settings.smtp_enabled and "send_email" in tool_resolution.final_tools):
        return
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


def _register_calcom_tool(agent: KaiAgent, tool_resolution: ToolResolution) -> None:
    """Register the Cal.com tools when enabled and requested."""
    from kai.agent.tools.calcom import get_calcom_settings

    calcom_settings = get_calcom_settings()
    if not (calcom_settings.calcom_enabled and "calcom" in tool_resolution.final_tools):
        return
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


def _register_optional_tools(
    agent: KaiAgent, tool_resolution: ToolResolution, bot: BaseBot
) -> tuple[MorphikClient | None, Engine | None]:
    """Register every enabled optional tool; return their closeable resources."""
    brain_client = _register_brain_tool(agent, tool_resolution)
    sql_engine = _register_sql_tool(agent, tool_resolution)
    _register_email_tool(agent, tool_resolution, bot)
    _register_calcom_tool(agent, tool_resolution)
    return brain_client, sql_engine


def _register_run(
    bot: BaseBot,
    instance_id: str,
    bot_name: str,
    user: str,
    settings: Settings,
    runs_registry: Callable[[str, Settings], RunRegistry],
) -> str | None:
    """Publish this instance in the run registry for ``kai bot tell``.

    Bots that opt out of tell return None from ``tell_endpoint()``.
    """
    endpoint = bot.tell_endpoint()
    if endpoint is None:
        return None
    run_id = generate_run_id()
    registry = runs_registry(instance_id, settings)
    registry.replace(
        RunRecord(
            run_id=run_id,
            endpoint=endpoint,
            hmac_key=bot.tell_hmac_key() or "",
            hmac_algorithm=bot.tell_hmac_algorithm(),
            pid=os.getpid(),
            pid_start=proc_start_time(os.getpid()),
            started_at=now_iso(),
        ),
    )
    print(f"KAI_RUN_ID={run_id}", flush=True)
    tell_cmd = f"kai bot tell {bot_name}" + (f" --user {user}" if user else "")
    card(
        "[bold]run id[/bold]",
        f"[bold yellow]{run_id}[/bold yellow]\n[{DIM}]use:[/{DIM}] "
        f'{tell_cmd} --run {run_id} -m "..."',
        border=WARN,
    )
    return run_id


async def _await_quietly(context: str, action: Callable[[], Awaitable[None]]) -> None:
    """Run an async cleanup action, logging instead of raising on failure."""
    try:
        await action()
    except Exception:
        logger.debug("%s raised during shutdown", context, exc_info=True)


def _run_quietly(context: str, action: Callable[[], None]) -> None:
    """Run a sync cleanup action, logging instead of raising on failure."""
    try:
        action()
    except Exception:
        logger.debug("%s raised during shutdown", context, exc_info=True)


async def _shutdown_resources(
    *,
    stop_task: asyncio.Task | None,
    agent: KaiAgent,
    brain_client: MorphikClient | None,
    sql_engine: Engine | None,
    run_id: str | None,
    instance_id: str,
    settings: Settings,
    runs_registry: Callable[[str, Settings], RunRegistry],
) -> None:
    """Stop the bot task and close every resource opened for the instance."""
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
        await _await_quietly("brain_client.close()", brain_client.close)
    if sql_engine is not None:
        _run_quietly("sql_engine.dispose()", sql_engine.dispose)
    if run_id is not None:
        _run_quietly(
            f"failed to unregister run {run_id}",
            lambda: runs_registry(instance_id, settings).remove(),
        )
    console.print(f"{GL_IDLE} [{DIM}]kai stopped[/{DIM}]")


async def run_instance(
    bot: BaseBot,
    agent: KaiAgent,
    tmpl: TemplateDef,
    tool_resolution: ToolResolution,
    instance_id: str,
    bot_name: str,
    user: str,
    settings: Settings,
    runs_registry: Callable[[str, Settings], RunRegistry],
) -> int:
    """Configure, run, and tear down one bot instance; return its exit code."""
    stop_task: asyncio.Task | None = None
    run_id: str | None = None
    brain_client: MorphikClient | None = None
    sql_engine: Engine | None = None

    try:
        bot.configure(agent, settings, template=tmpl, tools=tool_resolution)
    except (FileNotFoundError, ValueError, OSError) as exc:
        err_line(f"configuration error  {exc}")
        return 1

    brain_client, sql_engine = _register_optional_tools(agent, tool_resolution, bot)
    run_id = _register_run(bot, instance_id, bot_name, user, settings, runs_registry)

    loop = asyncio.get_running_loop()
    shutdown_requested = asyncio.Event()
    force_quit = asyncio.Event()
    run_task = asyncio.ensure_future(bot.run())

    def request_shutdown() -> None:
        """Request a graceful stop, forcing exit on a second signal."""
        if not shutdown_requested.is_set():
            shutdown_requested.set()
            nonlocal stop_task
            stop_task = asyncio.ensure_future(bot.stop())
            stop_task.add_done_callback(on_stop_done)
        elif not force_quit.is_set():
            force_quit.set()
            err_line("forcing exit")
            run_task.cancel()

    def on_stop_done(task: asyncio.Task) -> None:
        """Log an exception raised by the bot's stop coroutine."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("bot.stop() raised: %s", exc)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_shutdown)

    exit_code = 0
    try:
        await run_task
    except asyncio.CancelledError:
        err_line("forced shutdown")
    except BotStartupError as exc:
        err_line(f"startup failed  {exc}")
        exit_code = 1
    finally:
        await _shutdown_resources(
            stop_task=stop_task,
            agent=agent,
            brain_client=brain_client,
            sql_engine=sql_engine,
            run_id=run_id,
            instance_id=instance_id,
            settings=settings,
            runs_registry=runs_registry,
        )
    return exit_code
