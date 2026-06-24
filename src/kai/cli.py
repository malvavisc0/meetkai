import asyncio
import logging
import signal

import typer
from rich.console import Console

from kai.agent.core import KaiAgent
from kai.agent.goal import GoalManager
from kai.bots import list_bots, load_bot
from kai.bots.base import BaseBot
from kai.config.settings import get_settings
from kai.logging.logger import setup_logging

app = typer.Typer(name="kai", no_args_is_help=True)
console = Console()
logger = logging.getLogger(__name__)


class BotStartupError(Exception):
    """Raised when a bot fails during async startup."""


@app.command()
def start(
    bot_name: str = typer.Argument(..., help="Bot to start (e.g. 'waha')"),
    goal_text: str = typer.Option("", "--goal", "-g", help="Runtime goal"),
    language: str = typer.Option("", "--language", "-l", help="Override bot language"),
):
    """Start a bot. Blocks until SIGINT/SIGTERM."""
    settings = get_settings()
    setup_logging(log_dir=settings.log_dir)

    if language:
        settings.agent_language = language
        settings.agent_language_explicit = True

    try:
        bot = load_bot(bot_name)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    agent = KaiAgent(settings=settings, goal_manager=GoalManager(), namespace=bot_name)
    if goal_text:
        agent.goal_manager.set_goal(goal_text)

    console.print(f"[dim]starting[/dim] {bot.name}")

    async def _main() -> int:
        stop_task: asyncio.Task | None = None

        try:
            bot.configure(agent, settings)
        except (FileNotFoundError, ValueError, OSError) as exc:
            console.print(f"[red]configuration error:[/red] {exc}")
            return 1

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
                console.print("[red]forcing exit[/red]")
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
            console.print("[red]forced shutdown[/red]")
        except BotStartupError as exc:
            console.print(f"[red]startup failed:[/red] {exc}")
            exit_code = 1
        finally:
            if stop_task and not stop_task.done():
                try:
                    await asyncio.wait_for(stop_task, timeout=5.0)
                except (TimeoutError, Exception):
                    pass
            await agent.flush()
            console.print("[dim]stopped[/dim]")
        return exit_code

    exit_code = asyncio.run(_main())
    if exit_code:
        raise typer.Exit(exit_code)


@app.command(name="list")
def list_cmd():
    """List available bots."""
    bots = list_bots()
    if not bots:
        console.print("[dim]no bots found[/dim]")
        return
    for name in bots:
        console.print(name)


@app.command()
def status(bot_name: str = typer.Argument(..., help="Bot to query (e.g. 'waha')")):
    """Show a bot's transport/session status (delegates to the bot)."""
    setup_logging()
    try:
        bot = load_bot(bot_name)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if type(bot).status is BaseBot.status:
        console.print(f"[dim]{bot.name} does not support status[/dim]")
        raise typer.Exit(0)
    try:
        asyncio.run(bot.status())
    except Exception as exc:
        raise typer.Exit(1) from exc
