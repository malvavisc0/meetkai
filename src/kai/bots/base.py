import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kai.agent.context import ToolContext
from kai.agent.core import ActionResult, KaiAgent
from kai.agent.scheduler import TaskScheduler, TaskStore, build_task_tools
from kai.agent.tools.email import DEFAULT_DISPLAY_NAME
from kai.agent.tools.escalate import (
    EscalationStore,
    forward_to_cockpit,
    set_blacklist,
    set_cockpit_url,
    set_escalation_handler,
    set_escalation_secret,
    set_escalation_store,
    set_tool_context,
)
from kai.config.settings import Settings, get_settings
from kai.templates.resolver import ToolResolution
from kai.templates.schema import TemplateDef

logger = logging.getLogger(__name__)


class TaskAction(ActionResult):
    """Minimal action vocabulary for the generic scheduled-task path.

    A scheduled task either replies (delivering the result to the chat) or
    stays silent (no delivery). Bots with richer action vocabularies
    override :meth:`_execute_task` to pass their own ``ActionResult``
    subclass as ``output_cls``.
    """

    action: Literal["reply", "silent"]  # type: ignore[assignment]
    text: str | None = None


class TellResult(BaseModel):
    """Structured envelope returned by an operator ``/tell`` turn.

    The framework relays this verbatim as the ``/tell`` HTTP response; the
    ``kai tell`` CLI reads it and prints a human summary. It is built by the
    bot's ``_build_tell_result`` from a :class:`ChatResult`:

    - ``actions`` mirrors ``ChatResult.tool_calls`` (the side-effecting tool
      invocations the agent made — e.g. ``send_message``).
    - ``reply`` is the agent's natural-language ack, sourced from
      ``ChatResult.action.text`` (the turn resolved ``action == "console"``).
    """

    model_config = ConfigDict(frozen=True)

    ok: bool
    actions: list[dict] = Field(default_factory=list)
    reply: str = ""


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
        self.instance: str = ""  # per-run instance id; empty = use self.name
        self._agent: KaiAgent | None = None
        self._task_store: TaskStore | None = None
        self._task_scheduler: TaskScheduler | None = None
        self._escalation_store: EscalationStore | None = None
        self._tool_context: ToolContext | None = ToolContext()
        # Resolved template + tool set for this deployment. ``configure()``
        # always receives both (there is no ``None`` branch — the default
        # ``general`` template supplies a resolution, per TEMPLATES §6). Bare
        # bots constructed without ``configure()`` (tests/scripts) leave these
        # as ``None`` and the bot's own defaults apply.
        self._template: TemplateDef | None = None
        self._tool_resolution: ToolResolution | None = None

    @property
    def instance_id(self) -> str:
        """Per-instance identifier for file paths. Falls back to self.name."""
        return self.instance if self.instance else self.name

    def resolve_config_path(self) -> Path | None:
        """Resolve the operator's config override for this bot instance.

        Looks only at ``<configs_dir>/<name>.json`` — the deployment-specific
        override (e.g. ``configs/waha.json``). There is no packaged fallback:
        a bot with no override configured should fail loudly on missing
        settings (whitelist, language, SMTP, …) rather than silently run with
        made-up defaults.

        Returns the path if it exists, or ``None`` if it doesn't.
        """
        settings = get_settings()
        external = settings.configs_dir / f"{self.instance_id}.json"
        if external.is_file():
            logger.info("Loading bot config from external override: %s", external)
            return external
        return None

    def configure(
        self,
        agent: KaiAgent,
        settings: Settings,
        *,
        voice: str | None = None,
        template: TemplateDef,
        tools: ToolResolution,
    ) -> None:
        """Default configure: hold the agent + template/tool resolution.

        Override to load bot-specific config, register tools, etc. Subclasses
        that override this should call ``super().configure(...)`` (to capture
        the agent reference and resolution) and :meth:`setup_task_scheduler`.

        ``template`` is the resolved :class:`TemplateDef` (the default
        ``general`` template when ``--template`` is omitted). ``tools`` is the
        resolved :class:`ToolResolution`; bot-owned tool registration must
        check ``tools.final_tools`` membership before registering, so a
        template that omits a bot-owned tool actually omits it. There is no
        ``None`` branch — every deployment resolves a template.

        ``voice`` is an optional per-run override passed from the ``--voice``
        CLI flag; bots without voice support (the default) ignore it.
        """
        self._agent = agent
        self._template = template
        self._tool_resolution = tools

    def _has_tool(self, name: str) -> bool:
        """True when ``name`` is in the resolved tool set for this deployment.

        Bot-owned tool registration (``register_chat_history_tool``, the
        conversation tools, the task scheduler) gates on this so a template
        that omits a tool actually omits it. Returns True when no resolution
        is set (a bare bot without ``configure()``, e.g. in tests) so default
        behavior is preserved.
        """
        res = self._tool_resolution
        return res is None or name in res.final_tools

    def setup_task_scheduler(self, agent: KaiAgent, settings: Settings) -> None:
        """Create the task store + scheduler and register the task tools.

        Safe to call from a subclass ``configure()``. Idempotent: a second
        call is a no-op. The scheduler loop itself is started in
        :meth:`run` via :meth:`start_task_scheduler` and stopped in
        :meth:`stop`.

        Only wired when the template's resolved tool set includes at least one
        task tool (``schedule_task``/``list_tasks``/``cancel_task``): a focused
        template like ``order-status`` that omits them gets no scheduler at
        all, matching the template's declaration rather than always wiring it.
        ``settings.tasks_enabled`` is still honored as a global off-switch.
        """
        if not settings.tasks_enabled:
            return
        if self._task_scheduler is not None:
            return
        task_tool_names = ("schedule_task", "list_tasks", "cancel_task")
        if not any(self._has_tool(n) for n in task_tool_names):
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
            store_path = folder / f"{self.instance_id}.tasks.json"
            store_path.parent.mkdir(parents=True, exist_ok=True)
        self._task_store = TaskStore(store_path)
        self._task_scheduler = TaskScheduler(
            self._task_store,
            execute=self._execute_task,
            poll_interval=settings.tasks_poll_interval_seconds,
        )
        ctx = self._tool_context
        if ctx is not None:
            for tool in build_task_tools(
                self._task_scheduler,
                context=ctx,
                clarity_judge=self._judge_goal_clarity,
            ):
                agent.register_tool(tool)
        logger.info("Task scheduler wired for bot %s", self.name)

    def setup_escalation_store(self, settings: Settings) -> None:
        """Create the (JSON-persisted) escalation store for this bot.

        The store is created once (idempotent creation — a second call keeps
        the existing store so escalations aren't lost). The module-level
        ``_DYN`` state is *re-published* on every call, so a re-``configure()``
        after another bot overwrote the global restores this bot's store as
        the active one. (Single-bot-per-process is the production model; the
        re-publish keeps multi-bot tests and re-configuration correct.)

        Mirrors :meth:`setup_task_scheduler`'s path resolution so escalations
        survive restarts the same way tasks do.
        """
        if self._escalation_store is None:
            store_path = None
            if settings.escalations_folder is not None:
                folder = Path(settings.escalations_folder)
                if not folder.is_absolute():
                    folder = self.bot_dir / folder
                store_path = folder / f"{self.instance_id}.escalations.json"
                store_path.parent.mkdir(parents=True, exist_ok=True)
            self._escalation_store = EscalationStore(store_path)
        set_escalation_store(self._escalation_store)
        set_cockpit_url(settings.cockpit_url)
        set_escalation_secret(settings.cockpit_escalation_secret)
        logger.info("Escalation store wired for bot %s", self.name)

    def _wire_escalation_tools(self, settings: Settings, blacklist: list[str]) -> None:
        """Wire the escalate/blacklist tools' module-level state to this bot.

        Centralizes the ``setup_escalation_store`` + ``set_escalation_handler``
        + ``set_blacklist`` + ``set_tool_context`` sequence both bots need, so
        a wiring change lands in one place rather than drifting between waha
        and email.
        """
        self.setup_escalation_store(settings)
        set_escalation_handler(self.on_escalation)
        set_blacklist(blacklist)
        set_tool_context(self._tool_context)

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
        The agent's typed action decides what to deliver: ``reply`` sends
        ``action.text`` via :meth:`send_text`; ``silent`` (or an error)
        sends a short notice instead.
        """
        if self._agent is None:
            logger.warning("Bot %s has no agent; cannot execute task %s", self.name, task.id)
            return
        try:
            result = await self._agent.chat(
                task.goal,
                output_cls=TaskAction,
                conversation_id=task.chat_id,
                extra_system_context=(
                    "You are executing a scheduled task. Act on the goal "
                    "directly and reply with the result to the user."
                ),
            )
        except Exception:
            logger.exception("Agent execution failed for task %s", task.id)
            result = None
        if result is None or result.error or not result.action.text:
            await self.send_text(
                task.chat_id, f"\u23f0 Scheduled task ran but produced no reply: {task.goal}"
            )
            return
        if result.action.action == "silent":
            await self.send_text(
                task.chat_id, f"\u23f0 Scheduled task ran but produced no reply: {task.goal}"
            )
            return
        await self.send_text(task.chat_id, result.action.text or "")

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

    # --- Operator (/tell) surface ---------------------------------------
    #
    # The framework owns the ``/tell`` route + HMAC verification; each bot
    # that wants operator control supplies its endpoint + auth and overrides
    # :meth:`handle_operator`. A bot that returns ``None`` from
    # :meth:`tell_endpoint` opts out of ``tell`` entirely (no run registered,
    # no route wired).

    def tell_endpoint(self) -> str | None:
        """Return the HTTP endpoint a ``kai tell`` CLI should POST to.

        ``None`` means this bot does not support the operator ``/tell``
        surface. Returning a URL opts in: ``kai start`` registers a run_id
        for it and the framework wires the ``/tell`` route. Implementations
        should normalize a wildcard bind (``0.0.0.0``) to a loopback address
        the local CLI can actually reach.
        """
        return None

    def tell_hmac_key(self) -> str | None:
        """HMAC key ``kai tell`` must sign requests with for this bot."""
        return None

    def tell_hmac_algorithm(self) -> str:
        """HMAC algorithm (``sha256`` / ``sha512``) the ``/tell`` route verifies with."""
        return "sha512"

    async def handle_operator(self, message: str, *, persist: bool = False) -> TellResult:
        """Run an operator instruction as an agent turn and return a structured result.

        Override in a bot that supports ``tell``. The agent decides the
        delivery target through its structured action output (e.g.
        ``action.target``) — the bot dispatches it, there is no send tool.
        The default raises so a misconfigured ``/tell`` route (endpoint
        wired but no handler) fails loudly rather than silently no-oping.
        """
        raise NotImplementedError(f"{self.name} does not implement handle_operator()")

    def display_name(self) -> str:
        """The identity this bot presents as in outbound email ``From`` headers.

        Overridden by bots whose ``BotConfig`` carries a ``display_name``
        (waha, email) to return their own configured value; the default
        covers bots with no such concept.
        """
        return DEFAULT_DISPLAY_NAME

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
        return answer == "CLEAR"

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

    async def status_snapshot(self) -> dict:
        """Return a structured status snapshot for the ``/status`` route.

        Subclasses that support operator status override this to return a dict
        (e.g. ``{"session": {...}, "account": {...}}``). The default raises so
        the webhook ``/status`` route answers 404 for bots that opt out.
        """
        raise NotImplementedError(f"{self.name} does not implement status_snapshot()")

    async def ingest_event(self, event: dict) -> dict:
        """Receive a forwarded, already-normalized inbound event from the cockpit.

        The ``event`` dict is the ``model_dump()`` of a
        ``kai.cockpit.webhooks.NormalizedMessage`` (see its docstring for the
        contract): ``source`` (sender id / conversation_id), ``text``
        (plaintext body), ``metadata`` (provider-specific fields, e.g.
        ``message_id``/``subject``/``to``/``attachments``), and ``event`` (the
        event type, e.g. ``email.inbound``). A bot should return
        ``{"ok": False}`` for event types it doesn't act on.

        Default: not implemented (the bot opts out of centralized webhook
        ingest). A bot that consumes a ``WEBHOOK_TYPES`` entry overrides this
        to apply its own bespoke preprocessing and feed the result to its agent.
        """
        raise NotImplementedError(f"{self.name} does not implement ingest_event()")

    async def on_escalation(self, escalation) -> None:
        """Called when the ``escalate`` tool fires during an agent turn.

        Logs the escalation and forwards it to the cockpit's
        ``/api/escalations`` webhook (when ``KAI_COCKPIT_URL`` is set, i.e.
        the bot was spawned by the cockpit). The forwarding is best-effort —
        the escalation is already persisted locally, so a cockpit that's down
        just means the dashboard won't show it until the bot is restarted.

        Override to add transport-specific reactions (e.g. a WhatsApp DM to
        the operator for ``critical`` severity). Call ``await
        super().on_escalation(escalation)`` first so the cockpit forwarding
        still fires.

        ``escalation`` is a :class:`~kai.agent.tools.escalate.Escalation`.
        """
        level = "CRITICAL" if escalation.severity == "critical" else "WARNING"
        getattr(logger, level.lower())(
            "ESCALATION [%s] chat=%s reason=%s",
            escalation.severity,
            escalation.chat_id,
            escalation.reason,
        )
        await forward_to_cockpit(escalation)
