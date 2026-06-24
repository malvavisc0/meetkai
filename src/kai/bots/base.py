from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

from kai.agent.context import ToolContext
from kai.agent.core import KaiAgent, is_silent_reply
from kai.agent.scheduler import TaskScheduler, TaskStore, build_task_tools
from kai.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class BaseBot(ABC):
    """Concrete base class for bot plugins.

    Subclasses override hooks to customize behavior. The default
    ``configure()`` loads config + prompt and wires the agent.

    A lightweight task scheduler is wired automatically when
    ``settings.tasks_enabled`` is true: the store is persisted alongside the
    bot's history, the ``schedule_task`` / ``list_tasks`` / ``cancel_task``
    tools are registered on the agent, and a background loop executes due
    tasks. Each fired task's goal is run through ``agent.chat()`` (so the bot
    can use its tools) and the reply is delivered by overriding
    :meth:`send_text`. Tasks whose goal the LLM clarity judge deems unclear
    are refused at scheduling time.

    Constructor contract: ``Bot(bot_dir: Path)``
    """

    name: str = "base"

    def __init__(self, bot_dir: Path) -> None:
        self.bot_dir = bot_dir
        self._agent: KaiAgent | None = None
        self._task_store: TaskStore | None = None
        self._task_scheduler: TaskScheduler | None = None
        self._tool_context: ToolContext | None = None

    def resolve_config_path(self) -> Path | None:
        """Resolve the config file to load, external-first.

        Order:
        1. ``<configs_dir>/<name>.json`` — operator override at the repo/cwd
           root (e.g. ``configs/waha.json``). This is where deployment-specific
           settings (whitelists, language, …) belong, outside package source.
        2. ``<bot_dir>/config.json`` — the packaged default shipped with the
           bot, used as a fallback so the bot works out of the box.

        Returns the first existing path, or ``None`` if neither exists.
        """
        settings = get_settings()
        external = settings.configs_dir / f"{self.name}.json"
        if external.is_file():
            logger.info("Loading bot config from external override: %s", external)
            return external
        packaged = self.bot_dir / "config.json"
        if packaged.is_file():
            logger.info("Loading bot config from packaged default: %s", packaged)
            return packaged
        return None

    def configure(self, agent: KaiAgent, settings: Settings) -> None:
        """Default configure: hold the agent for task execution.

        Override to load bot-specific config, register tools, etc. Subclasses
        that override this should call ``super().configure(...)`` (to capture
        the agent reference) and :meth:`setup_task_scheduler`.
        """
        self._agent = agent

    def setup_task_scheduler(self, agent: KaiAgent, settings: Settings) -> None:
        """Create the task store + scheduler and register the task tools.

        Safe to call from a subclass ``configure()``. Idempotent: a second
        call is a no-op. The scheduler loop itself is started in
        :meth:`run` via :meth:`start_task_scheduler` and stopped in
        :meth:`stop`.
        """
        if not settings.tasks_enabled:
            return
        if self._task_scheduler is not None:
            return

        self._agent = agent
        store_path = None
        if settings.tasks_folder is not None:
            # Anchor relative folders to the bot's own directory so the store
            # lives "alongside the bot" and does not depend on the process CWD
            # (a relative path would otherwise resolve differently each time the
            # bot is started from a different directory, silently losing tasks).
            folder = Path(settings.tasks_folder)
            if not folder.is_absolute():
                folder = self.bot_dir / folder
            store_path = folder / f"{self.name}.tasks.json"
            store_path.parent.mkdir(parents=True, exist_ok=True)
        self._task_store = TaskStore(store_path)
        self._task_scheduler = TaskScheduler(
            self._task_store,
            execute=self._execute_task,
            poll_interval=settings.tasks_poll_interval_seconds,
        )
        self._tool_context = ToolContext()
        for tool in build_task_tools(
            self._task_scheduler,
            context=self._tool_context,
            clarity_judge=self._judge_goal_clarity,
        ):
            agent.register_tool(tool)
        logger.info("Task scheduler wired for bot %s", self.name)

    def set_task_context(
        self, chat_id: str, owner_id: str = "", tz_hint: str | None = None
    ) -> None:
        """Set the chat context the task tools will operate on.

        Call this right before ``agent.chat()`` for an inbound message so
        ``schedule_task`` etc. target the originating chat.
        """
        ctx = self._tool_context
        if ctx is None:
            return
        ctx.set(chat_id=chat_id, owner_id=owner_id, tz_hint=tz_hint)

    async def _execute_task(self, task) -> None:
        """Execute a fired task by feeding its goal back to the agent.

        The agent runs with its normal tools EXCEPT that ``schedule_task``
        refuses to run while a task is executing (re-entrancy guard in the
        scheduler), so a fired task can't spawn a replacement and recurse.
        The agent's reply is delivered via :meth:`send_text`; if there's no
        reply (silent/error), a short notice is sent instead.
        """
        if self._agent is None:
            logger.warning("Bot %s has no agent; cannot execute task %s", self.name, task.id)
            return
        try:
            reply = await self._agent.chat(
                task.goal,
                conversation_id=task.chat_id,
                extra_system_context=(
                    "You are executing a scheduled task. Act on the goal "
                    "directly and reply with the result to the user."
                ),
            )
        except Exception:
            logger.exception("Agent execution failed for task %s", task.id)
            reply = None
        if not reply or is_silent_reply(reply):
            reply = f"⏰ Scheduled task ran but produced no reply: {task.goal}"
        await self.send_text(task.chat_id, reply)

    async def send_text(self, chat_id: str, text: str) -> None:
        """Deliver a text message to ``chat_id``.

        Default implementation logs a warning — subclasses with a transport
        (e.g. waha) override this to actually send the message.
        """
        logger.warning(
            "Bot %s has no send_text implementation; dropping message to %s",
            self.name,
            chat_id,
        )

    async def _judge_goal_clarity(self, goal: str) -> bool:
        """LLM-backed check that a goal is clear enough to act on autonomously.

        Asks the agent's LLM a yes/no question via :meth:`KaiAgent.complete`;
        returns True only on an affirmative answer. On any failure (no agent,
        LLM error, unparseable answer) returns False so unclear goals are
        never scheduled.
        """
        if self._agent is None:
            return False
        trimmed = goal.strip()
        prompt = (
            "You judge whether a task goal is clear and specific enough for an "
            "assistant to act on autonomously without asking further questions.\n\n"
            f"Goal: {trimmed}\n\n"
            "Reply with exactly one word: CLEAR or UNCLEAR. A goal is CLEAR "
            "when it states what to do concretely enough to act on; UNCLEAR "
            "when it is vague, ambiguous, or missing the actual action."
        )
        answer = (await self._agent.complete(prompt)).strip().upper()
        return answer.startswith("CLEAR")

    def start_task_scheduler(self) -> None:
        if self._task_scheduler is not None:
            self._task_scheduler.start()

    async def stop_task_scheduler(self) -> None:
        if self._task_scheduler is not None:
            await self._task_scheduler.stop()

    @abstractmethod
    async def run(self) -> None: ...

    async def stop(self) -> None:
        await self.stop_task_scheduler()

    async def status(self) -> None:
        raise NotImplementedError(f"{self.name} does not implement status()")
