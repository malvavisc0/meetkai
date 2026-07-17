"""Scheduled-task scheduler for bots.

A :class:`TaskStore` persists one-shot tasks as JSON; a :class:`TaskScheduler`
polls the store and executes due tasks through a transport-agnostic
:func:`ExecuteCallback`. :func:`build_task_tools` exposes ``schedule_task`` /
``list_tasks`` / ``cancel_task`` to the model, scoped to the current chat via
:class:`ToolContext`.

Two behavioral guarantees:

- **A task's goal must be clear.** Before persisting, an optional LLM judge
  decides whether the goal is clear enough to act on autonomously. If not, the
  tool refuses and the model is expected to ask the user for a clearer goal.

- **Fired tasks cannot create new tasks.** When the scheduler executes a task
  it sets a re-entrancy flag (a :class:`contextvars.ContextVar`) for the whole
  execution; ``schedule_task`` refuses to run while that flag is set. This
  prevents the "schedule a task that schedules a task" recursion at the tool
  level, regardless of what the model tries to do.
"""

import asyncio
import json
import logging
import uuid
from calendar import monthrange
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from llama_index.core.tools import FunctionTool
from pydantic import BaseModel, ConfigDict, Field

from kai.agent.context import ToolContext
from kai.agent.helpers import (
    parse_iso,
    parse_relative,
    resolve_tz,
    to_iso,
    try_iso,
    utcnow,
)

logger = logging.getLogger(__name__)

# Closed set of recurrence kinds. RepeatKind is the type the model, the
# scheduler, and the schedule_task tool all share; REPEAT_KINDS validates
# values loaded from persisted JSON (which may predate or diverge from the
# Literal) so one bad value can't poison a whole task record.
RepeatKind = Literal["none", "daily", "weekly", "monthly"]
REPEAT_KINDS: tuple[RepeatKind, ...] = ("none", "daily", "weekly", "monthly")


def _coerce_repeat(value: object) -> RepeatKind:
    """Normalize a persisted ``repeat`` value to a valid RepeatKind.

    Old or hand-edited stores may carry an unknown string; fall back to
    ``"none"`` instead of letting pydantic reject the whole record.
    """
    text = str(value) if value is not None else "none"
    return cast(RepeatKind, text) if text in REPEAT_KINDS else "none"


# Farthest we'll ever schedule into the future. Stops the model booking
# tasks that can never fire usefully.
_MAX_FUTURE = timedelta(days=365 * 5)

# Re-entrancy guard: set while a fired task is executing so the agent, which
# runs inside that same asyncio context, cannot create a replacement task and
# recurse. Read by schedule_task; set only by the scheduler's execution path.
_EXECUTING_TASK = ContextVar("kai_executing_task", default=False)


def parse_when(when: str, *, tz_hint: str | None = None) -> datetime:
    """Parse a due time into an aware UTC datetime.

    Accepts ISO-8601 timestamps (``2026-06-18T15:30Z``), ``YYYY-MM-DD HH:MM``
    (interpreted in ``tz_hint``, default UTC), or relative offsets
    (``in 90m`` / ``in 2h`` / ``in 1d`` / ``in 1h30m``). Raises ``ValueError``
    on anything it can't parse.
    """
    text = (when or "").strip()
    if not text:
        raise ValueError("when is required")

    tz = resolve_tz(tz_hint)

    rel = parse_relative(text)
    if rel is not None:
        return (utcnow() + rel).astimezone(UTC)

    iso = try_iso(text, tz)
    if iso is not None:
        return iso.astimezone(UTC)

    raise ValueError(
        f"Could not parse time {when!r}. Use ISO (2026-06-18T15:30Z), "
        "'YYYY-MM-DD HH:MM', or a relative 'in 2h' / 'in 30m'."
    )


class Task(BaseModel):
    """A single scheduled task.

    Fires once at ``due_at`` (UTC) unless recurrence is set. At fire time the
    scheduler calls the registered :data:`ExecuteCallback` with the task; the
    callback is expected to act on ``goal`` (e.g. feed it back into the agent
    and send the reply). ``chat_id`` is the conversation the task was created
    in; ``owner_id`` records who asked for it (e.g. a WhatsApp JID).

    Recurrence: ``repeat`` controls the period (daily/weekly/monthly).
    ``interval`` is the step (every N days/weeks/months). For weekly recurrence,
    ``weekdays`` can restrict to specific days (0=Mon..6=Sun). ``until`` and
    ``count`` limit the recurrence. ``occurrences`` tracks how many times the
    task has fired.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    chat_id: str
    goal: str
    due_at: datetime
    owner_id: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    repeat: RepeatKind = "none"
    interval: int = 1
    weekdays: tuple[int, ...] | None = None
    until: datetime | None = None
    count: int | None = None
    occurrences: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = self.model_dump()
        d["due_at"] = to_iso(self.due_at)
        d["created_at"] = to_iso(self.created_at)
        if self.until:
            d["until"] = to_iso(self.until)
        if self.weekdays is not None:
            d["weekdays"] = list(self.weekdays)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        goal = str(data.get("goal") or data.get("message") or "")
        weekdays_data = data.get("weekdays")
        weekdays = tuple(weekdays_data) if weekdays_data else None
        return cls(
            id=str(data["id"]),
            chat_id=str(data["chat_id"]),
            goal=goal,
            due_at=parse_iso(str(data["due_at"])),
            owner_id=str(data.get("owner_id", "")),
            created_at=parse_iso(str(data["created_at"])) if data.get("created_at") else utcnow(),
            repeat=_coerce_repeat(data.get("repeat", "none")),
            interval=int(data.get("interval", 1)),
            weekdays=weekdays,
            until=parse_iso(str(data["until"])) if data.get("until") else None,
            count=int(data["count"]) if data.get("count") else None,
            occurrences=int(data.get("occurrences", 0)),
        )


def compute_next_due(task: Task) -> datetime | None:
    """Compute the next fire time for a recurring task.

    Returns None if the task has exceeded its until/count limit.
    """
    if task.repeat == "none":
        return None
    next_dt = task.due_at
    interval = max(1, task.interval)
    if task.repeat == "daily":
        next_dt = next_dt + timedelta(days=interval)
    elif task.repeat == "weekly":
        if task.weekdays:
            for _ in range(7 * interval + 1):
                next_dt = next_dt + timedelta(days=1)
                if next_dt.weekday() in task.weekdays:
                    break
        else:
            next_dt = next_dt + timedelta(weeks=interval)
    elif task.repeat == "monthly":
        year = next_dt.year
        month = next_dt.month + interval
        while month > 12:
            month -= 12
            year += 1
        day = min(next_dt.day, monthrange(year, month)[1])
        next_dt = next_dt.replace(year=year, month=month, day=day)
    if task.until and next_dt > task.until:
        return None
    if task.count is not None and task.occurrences + 1 >= task.count:
        return None
    return next_dt


class TaskStore:
    """Persistent store of scheduled tasks.

    Writes are atomic (temp file + replace). The on-disk format is a JSON
    object ``{"tasks": [...]}`` so it survives restarts and is human-readable.

    The lock is created lazily per event loop (see :meth:`_lock_for`) rather
    than in ``__init__``: a store is often reused across several
    ``asyncio.run`` calls (notably in tests), and an ``asyncio.Lock`` bound to
    a dead loop would otherwise raise ``RuntimeError`` on the second call.
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._tasks: dict[str, Task] = {}
        self._lock: asyncio.Lock | None = None
        self._load()

    def _lock_for(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            for item in raw.get("tasks", []):
                if not isinstance(item, dict) or "id" not in item:
                    continue
                task = Task.from_dict(item)
                self._tasks[task.id] = task
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("Failed to load tasks from %s: %s", self._path, exc)

    def _save_locked(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {"tasks": [t.to_dict() for t in self._tasks.values()]}
            tmp = Path(f"{self._path}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            logger.warning("Failed to save tasks to %s: %s", self._path, exc)

    async def add(self, task: Task) -> None:
        async with self._lock_for():
            self._tasks[task.id] = task
            self._save_locked()

    async def cancel(self, task_id: str, *, chat_id: str) -> bool:
        """Cancel a task, but only if it belongs to ``chat_id``.

        ``chat_id`` is required (not optional) so a caller can't accidentally
        cancel a task across chats by omitting it.
        """
        async with self._lock_for():
            task = self._tasks.get(task_id)
            if task is None:
                return False
            if task.chat_id != chat_id:
                return False
            del self._tasks[task_id]
            self._save_locked()
            return True

    async def list_for(self, chat_id: str | None = None, owner_id: str | None = None) -> list[Task]:
        async with self._lock_for():
            tasks = list(self._tasks.values())
        tasks.sort(key=lambda t: t.due_at)
        return [
            t
            for t in tasks
            if (chat_id is None or t.chat_id == chat_id)
            and (owner_id is None or t.owner_id == owner_id)
        ]

    async def pop_due(self, now: datetime | None = None) -> list[Task]:
        """Return all due tasks, advancing or deleting them as appropriate.

        For one-shot tasks (repeat="none"): removed from store.
        For recurring tasks: advanced to next due_at (or removed if past until/count).
        Returns tasks with their fire-time due_at (before any advance).
        """
        now = now or utcnow()
        async with self._lock_for():
            due = [t for t in self._tasks.values() if t.due_at <= now]
            fired: list[Task] = []
            for t in due:
                self._tasks.pop(t.id, None)
                fired.append(t)
                if t.repeat != "none":
                    next_dt = compute_next_due(t)
                    if next_dt:
                        t2 = t.model_copy(
                            update={"due_at": next_dt, "occurrences": t.occurrences + 1}
                        )
                        self._tasks[t2.id] = t2
            if due:
                self._save_locked()
            return fired

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]


# A fired task is handed to this callback. ``chat_id`` is the originating
# conversation; ``goal`` is the clear goal the bot must act on. The callback
# is responsible for execution (e.g. agent.chat) and delivery; the scheduler
# stays transport-agnostic.
ExecuteCallback = Callable[[Task], Awaitable[None]]

# How far past ``due_at`` a task can be before we flag it as overdue in the
# delivered message (e.g. fired after a restart delay). Keeps on-time tasks
# from showing the "overdue" label just because the poll interval slipped.
_OVERDUE_GRACE = timedelta(minutes=1)

# Hard floor on goal length before the LLM judge even runs. Cheap, deterministic
# first line of defense: a 2-word goal is never "clear".
_MIN_GOAL_CHARS = 8


class TaskScheduler:
    """Background loop that executes due tasks via a registered callback.

    ``start()`` spawns a polling task; ``stop()`` cancels it cleanly. Bots
    provide the :data:`ExecuteCallback` — the scheduler is transport-agnostic.
    A single task failing to execute is logged and swallowed so one bad run
    doesn't kill the loop (fire-once: the task is removed regardless).
    """

    def __init__(
        self,
        store: TaskStore,
        execute: ExecuteCallback,
        *,
        poll_interval: float = 5.0,
    ) -> None:
        self._store = store
        self._execute = execute
        self._poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        logger.info("Task scheduler started (poll=%.1fs)", self._poll_interval)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Task scheduler tick failed")
            await asyncio.sleep(self._poll_interval)

    async def _tick(self) -> None:
        due = await self._store.pop_due()
        if not due:
            return
        now = utcnow()
        for task in due:
            await self._execute_one(task, now=now)

    async def _execute_one(self, task: Task, *, now: datetime | None = None) -> None:
        """Execute a fired task inside a recursion-guarded context.

        Sets ``_EXECUTING_TASK`` so any ``schedule_task`` call made by the
        agent during this execution is refused — the fired task cannot spawn a
        replacement and recurse. A failure here is logged and swallowed so
        the poll loop survives.
        """
        now = now or utcnow()
        token = _EXECUTING_TASK.set(True)
        try:
            await self._execute(task)
            logger.info("Task %s executed for %s", task.id, task.chat_id)
        except Exception:
            logger.exception("Failed to execute task %s for %s", task.id, task.chat_id)
        finally:
            _EXECUTING_TASK.reset(token)

    @staticmethod
    def render_due(task: Task, *, now: datetime | None = None) -> str:
        now = now or utcnow()
        when = task.due_at.strftime("%Y-%m-%d %H:%M UTC")
        late = now - task.due_at > _OVERDUE_GRACE
        prefix = "⏰ Task (overdue, was due " if late else "⏰ Task ("
        return f"{prefix}{when}): {task.goal}"

    async def schedule(
        self,
        *,
        chat_id: str,
        goal: str,
        due_at: datetime,
        owner_id: str = "",
        repeat: RepeatKind = "none",
        interval: int = 1,
        weekdays: tuple[int, ...] | None = None,
        until: datetime | None = None,
        count: int | None = None,
    ) -> Task:
        task = Task(
            id=TaskStore.new_id(),
            chat_id=chat_id,
            goal=goal,
            due_at=due_at,
            owner_id=owner_id,
            repeat=repeat,
            interval=interval,
            weekdays=weekdays,
            until=until,
            count=count,
            occurrences=0,
        )
        await self._store.add(task)
        logger.info("Scheduled task %s for %s in %s", task.id, to_iso(due_at), chat_id)
        return task

    async def cancel(self, task_id: str, *, chat_id: str) -> bool:
        return await self._store.cancel(task_id, chat_id=chat_id)

    async def list_for(self, chat_id: str | None = None, owner_id: str | None = None) -> list[Task]:
        return await self._store.list_for(chat_id=chat_id, owner_id=owner_id)


# Optional LLM-based clarity judge. Returns True only when the goal is
# clear enough to act on autonomously. Returns True when unset.
ClarityJudge = Callable[[str], Awaitable[bool]]


def build_task_tools(
    scheduler: TaskScheduler,
    *,
    context: ToolContext,
    clarity_judge: ClarityJudge | None = None,
) -> list[FunctionTool]:
    """Build the task tools bound to ``scheduler`` and a context source.

    ``context`` supplies the current chat_id / owner for each call so the
    model can't address tasks to chats it has never seen.
    ``clarity_judge`` is an optional async LLM-backed check that the goal is
    clear; when it returns False the tool refuses to schedule.
    """

    async def schedule_task(
        when: str,
        goal: str,
        repeat: RepeatKind = "none",
        weekdays: str | None = None,
        until: str | None = None,
        count: int | None = None,
    ) -> dict:
        """Schedule a task the bot will execute autonomously later.

        Use for "in 2 hours, do X" or "tomorrow at 9am do Y". The goal must
        be clear and specific enough to act on without further questions;
        if it isn't, the tool will refuse and you should ask the user to
        clarify what they want.

        For recurring tasks, set repeat to daily, weekly, or monthly. For
        weekly recurrence on specific days, provide weekdays as a comma-
        separated list: "mon,wed,fri".

        Args:
            when: When to execute. Absolute ISO ("2026-06-18T15:30Z"), a
                local "YYYY-MM-DD HH:MM", or a relative "in 2h" / "in 30m".
            goal: A clear, self-contained statement of what to do. Must be
                specific enough that the bot can act on it without asking.
            repeat: "none" (default, one-shot), "daily", "weekly", or "monthly".
            weekdays: For weekly recurrence, comma-separated day abbreviations
                (e.g. "mon,wed,fri"). Case-insensitive. Ignored for other repeats.
            until: ISO timestamp when recurrence should stop (inclusive).
            count: Maximum number of times the task should fire.
        """
        if _EXECUTING_TASK.get():
            return {
                "error": (
                    "refusing to schedule a task while executing another task (this would recurse)."
                )
            }

        goal_text = (goal or "").strip()
        if len(goal_text) < _MIN_GOAL_CHARS:
            return {
                "error": (
                    "the goal is too short to be clear. Ask the user what exactly "
                    "they want done, then try again."
                )
            }

        try:
            due = parse_when(when, tz_hint=context.current().tz_hint)
        except ValueError as exc:
            return {"error": str(exc)}
        now = utcnow()
        if due <= now:
            return {"error": f"that time is in the past ({due.isoformat()})"}
        if due - now > _MAX_FUTURE:
            return {"error": "that's too far in the future (max ~5 years)"}

        if clarity_judge is not None:
            try:
                clear = await clarity_judge(goal_text)
            except Exception:
                logger.warning("clarity judge failed; assuming goal is unclear", exc_info=True)
                clear = False
            if not clear:
                return {
                    "error": (
                        "the goal isn't clear enough to act on autonomously. "
                        "Ask the user to state exactly what they want done."
                    )
                }

        weekdays_tuple: tuple[int, ...] | None = None
        if repeat == "weekly" and weekdays:
            day_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
            days: list[int] = []
            for part in weekdays.lower().split(","):
                part = part.strip()
                if part in day_map:
                    days.append(day_map[part])
            if days:
                weekdays_tuple = tuple(sorted(days))

        until_dt: datetime | None = None
        if until:
            try:
                until_dt = parse_when(until, tz_hint=context.current().tz_hint)
            except ValueError:
                return {"error": f"could not parse until time: {until}"}

        ctx = context.current()
        task = await scheduler.schedule(
            chat_id=ctx.chat_id,
            goal=goal_text,
            due_at=due,
            owner_id=ctx.owner_id,
            repeat=repeat,
            weekdays=weekdays_tuple,
            until=until_dt,
            count=count,
        )
        rec_parts = [f" ({repeat}"]
        if count:
            rec_parts.append(f", {count} times")
        if until:
            rec_parts.append(f" until {until}")
        rec_parts.append(")")
        rec_info = "".join(rec_parts) if repeat != "none" else ""
        return {
            "id": task.id,
            "fires_at": task.due_at.strftime("%Y-%m-%d %H:%M UTC"),
            "chat": task.chat_id,
            "recurring": rec_info or None,
        }

    async def list_tasks(include_past: bool = False) -> dict:
        """List pending tasks in this chat.

        Args:
            include_past: If True, also include tasks that were already
                fired (by default only pending ones are returned).
        """
        ctx = context.current()
        tasks = await scheduler.list_for(chat_id=ctx.chat_id)
        now = utcnow()
        items = [
            {
                "id": t.id,
                "goal": t.goal,
                "fires_at": t.due_at.strftime("%Y-%m-%d %H:%M UTC"),
                "overdue": t.due_at <= now,
            }
            for t in tasks
            if include_past or t.due_at > now
        ]
        return {"count": len(items), "tasks": items}

    async def cancel_task(task_id: str) -> dict:
        """Cancel a pending task by its id (from list_tasks).

        Args:
            task_id: The task id returned by schedule_task.
        """
        ctx = context.current()
        ok = await scheduler.cancel(task_id, chat_id=ctx.chat_id)
        if not ok:
            return {"error": "no task with that id in this chat"}
        return {"cancelled": True, "id": task_id}

    return [
        FunctionTool.from_defaults(
            fn=schedule_task,
            name="schedule_task",
            description=(
                "Schedule a task the bot will execute autonomously at a later "
                "time. The goal must be clear and specific. Use for 'in 2 "
                "hours do X' or 'tomorrow at 9am do Y'. For recurring "
                "reminders set repeat='daily'/'weekly'/'monthly'; for "
                "specific weekdays use weekdays='mon,wed,fri'. Use count "
                "or until to limit recurrence."
            ),
        ),
        FunctionTool.from_defaults(
            fn=list_tasks,
            name="list_tasks",
            description="List pending tasks in this chat.",
        ),
        FunctionTool.from_defaults(
            fn=cancel_task,
            name="cancel_task",
            description="Cancel a pending task by id.",
        ),
    ]
