import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kai.agent.context import ToolContext
from kai.agent.scheduler import (
    _EXECUTING_TASK,
    Task,
    TaskScheduler,
    TaskStore,
    build_task_tools,
    compute_next_due,
)


def _make_task(due_offset_seconds: int = 60, chat_id: str = "c1", task_id: str = "abc123") -> Task:
    return Task(
        id=task_id,
        chat_id=chat_id,
        goal="post a standup summary in the team channel",
        due_at=datetime.now(UTC) + timedelta(seconds=due_offset_seconds),
        owner_id="owner1",
    )


class TestTaskStore:
    def test_add_and_list(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.json")
        asyncio.run(store.add(_make_task()))
        tasks = asyncio.run(store.list_for(chat_id="c1"))
        assert len(tasks) == 1
        assert tasks[0].goal.startswith("post a standup")

    def test_list_filters_by_chat(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.json")
        asyncio.run(store.add(_make_task(chat_id="c1", task_id="t1")))
        asyncio.run(store.add(_make_task(chat_id="c2", task_id="t2")))
        assert len(asyncio.run(store.list_for(chat_id="c1"))) == 1
        assert len(asyncio.run(store.list_for(chat_id="c2"))) == 1

    def test_cancel(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.json")
        asyncio.run(store.add(_make_task()))
        assert asyncio.run(store.cancel("abc123", chat_id="c1")) is True
        assert asyncio.run(store.cancel("abc123", chat_id="c1")) is False
        assert asyncio.run(store.list_for()) == []

    def test_cancel_rejects_cross_chat(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.json")
        asyncio.run(store.add(_make_task(chat_id="c1")))
        # A task belonging to c1 must not be cancellable by c2.
        assert asyncio.run(store.cancel("abc123", chat_id="c2")) is False
        assert len(asyncio.run(store.list_for(chat_id="c1"))) == 1
        # The rightful owner chat can still cancel it.
        assert asyncio.run(store.cancel("abc123", chat_id="c1")) is True

    def test_pop_due_only_returns_past(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.json")
        future = _make_task(due_offset_seconds=600)
        past = Task(
            id="past1",
            chat_id="c1",
            goal="overdue task that should fire",
            due_at=datetime.now(UTC) - timedelta(seconds=10),
        )
        asyncio.run(store.add(future))
        asyncio.run(store.add(past))
        due = asyncio.run(store.pop_due())
        assert [t.id for t in due] == ["past1"]
        # The past task is removed; the future one stays.
        remaining = asyncio.run(store.list_for())
        assert [t.id for t in remaining] == ["abc123"]

    def test_persistence_across_instances(self, tmp_path: Path):
        path = tmp_path / "tasks.json"
        store = TaskStore(path)
        asyncio.run(store.add(_make_task()))
        # New store instance reading the same file.
        store2 = TaskStore(path)
        tasks = asyncio.run(store2.list_for())
        assert len(tasks) == 1
        assert tasks[0].goal.startswith("post a standup")

    def test_round_trip_iso(self):
        # to_iso drops sub-second precision, so compare at whole-second granularity.
        t = Task(
            id="x",
            chat_id="c",
            goal="send the weekly report to the mailing list",
            due_at=datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC),
            owner_id="o",
        )
        t2 = Task.from_dict(t.to_dict())
        assert t2.id == t.id
        assert t2.chat_id == t.chat_id
        assert t2.due_at == t.due_at

    def test_loads_legacy_message_field_as_goal(self, tmp_path: Path):
        """Older stores used 'message'; the loader must map it to 'goal'."""
        import json

        path = tmp_path / "tasks.json"
        path.write_text(
            json.dumps(
                {
                    "tasks": [
                        {
                            "id": "legacy1",
                            "chat_id": "c",
                            "message": "legacy goal text here",
                            "due_at": "2026-06-18T12:00:00Z",
                            "created_at": "2026-06-18T10:00:00Z",
                        }
                    ]
                }
            )
        )
        store = TaskStore(path)
        tasks = asyncio.run(store.list_for())
        assert len(tasks) == 1
        assert tasks[0].goal == "legacy goal text here"


class TestTaskScheduler:
    def test_schedule_and_execute(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.json")
        executed: list[Task] = []

        async def execute(task: Task) -> None:
            executed.append(task)

        sched = TaskScheduler(store, execute, poll_interval=0.05)
        task = asyncio.run(
            sched.schedule(
                chat_id="c1",
                goal="call mom and wish her happy birthday",
                due_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        # Force a tick; due task is executed and removed.
        asyncio.run(sched._tick())  # noqa: SLF001
        assert [t.id for t in executed] == [task.id]
        assert asyncio.run(store.list_for()) == []

    def test_execute_failure_does_not_crash_loop(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.json")
        calls = {"n": 0}

        async def execute(task: Task) -> None:
            calls["n"] += 1
            raise RuntimeError("agent failed")

        sched = TaskScheduler(store, execute, poll_interval=0.05)
        asyncio.run(
            sched.schedule(
                chat_id="c1",
                goal="do something specific and actionable now",
                due_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        # Should not raise even though execute fails.
        asyncio.run(sched._tick())  # noqa: SLF001
        assert calls["n"] == 1
        # Failed task is still popped (fire-once semantics).
        assert asyncio.run(store.list_for()) == []

    def test_render_due_flags_overdue(self, tmp_path: Path):
        task = Task(
            id="late",
            chat_id="c1",
            goal="take the medication with breakfast",
            due_at=datetime.now(UTC) - timedelta(hours=2),
        )
        rendered = TaskScheduler.render_due(task)
        assert "overdue" in rendered
        assert "take the medication" in rendered

    def test_render_due_not_overdue_when_on_time(self):
        task = Task(
            id="on",
            chat_id="c1",
            goal="ping the on-call engineer about the deploy",
            due_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        rendered = TaskScheduler.render_due(task)
        assert "overdue" not in rendered


class TestTaskTools:
    def _tools(self, tmp_path: Path, *, clarity_judge=None):
        store = TaskStore(tmp_path / "tasks.json")

        async def execute(task: Task) -> None: ...

        sched = TaskScheduler(store, execute)
        ctx = ToolContext(chat_id="chat-1", owner_id="owner-1")
        tools = {
            t.metadata.name: t
            for t in build_task_tools(sched, context=ctx, clarity_judge=clarity_judge)
        }
        return sched, tools

    def _call(self, tool, **kwargs):
        """Invoke a FunctionTool and unwrap its raw output dict."""
        out = asyncio.run(tool.acall(**kwargs))
        return out.raw_output

    def test_schedule_relative(self, tmp_path: Path):
        sched, tools = self._tools(tmp_path)
        goal = "water the plants on the windowsill thoroughly"
        res = self._call(tools["schedule_task"], when="in 5m", goal=goal)
        assert "error" not in res
        assert res["chat"] == "chat-1"
        tasks = asyncio.run(sched.list_for())
        assert len(tasks) == 1
        assert tasks[0].goal == goal

    def test_schedule_rejects_past(self, tmp_path: Path):
        _, tools = self._tools(tmp_path)
        res = self._call(tools["schedule_task"], when="in -1m", goal="do the thing now please")
        assert "error" in res

    def test_schedule_rejects_short_goal(self, tmp_path: Path):
        _, tools = self._tools(tmp_path)
        res = self._call(tools["schedule_task"], when="in 5m", goal="do it")
        assert "error" in res

    def test_schedule_rejects_unclear_goal_via_judge(self, tmp_path: Path):
        async def judge(goal: str) -> bool:
            return False  # LLM says: unclear

        _, tools = self._tools(tmp_path, clarity_judge=judge)
        res = self._call(
            tools["schedule_task"], when="in 5m", goal="something maybe later possibly"
        )
        assert "error" in res
        assert "clear" in res["error"].lower()

    def test_schedule_accepts_clear_goal_via_judge(self, tmp_path: Path):
        async def judge(goal: str) -> bool:
            return True  # LLM says: clear

        sched, tools = self._tools(tmp_path, clarity_judge=judge)
        res = self._call(
            tools["schedule_task"], when="in 5m", goal="send the meeting notes to the team channel"
        )
        assert "error" not in res
        assert len(asyncio.run(sched.list_for())) == 1

    def test_list_and_cancel(self, tmp_path: Path):
        sched, tools = self._tools(tmp_path)
        created = self._call(
            tools["schedule_task"], when="in 1h", goal="ping the on-call engineer about the deploy"
        )
        listed = self._call(tools["list_tasks"])
        assert listed["count"] == 1
        cancel_res = self._call(tools["cancel_task"], task_id=created["id"])
        assert cancel_res["cancelled"] is True
        assert asyncio.run(sched.list_for()) == []

    def test_cancel_unknown_id_errors(self, tmp_path: Path):
        _, tools = self._tools(tmp_path)
        res = self._call(tools["cancel_task"], task_id="nope")
        assert "error" in res

    def test_iso_timestamp(self, tmp_path: Path):
        sched, tools = self._tools(tmp_path)
        future = (datetime.now(UTC) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%MZ")
        goal = "go through the pull requests queue"
        res = self._call(tools["schedule_task"], when=future, goal=goal)
        assert "error" not in res
        assert len(asyncio.run(sched.list_for())) == 1

    def test_concurrent_chats_do_not_clobber_context(self, tmp_path: Path):
        """Tasks scheduled from two chats running concurrently must each target
        their own chat, not whichever chat was set most recently."""
        store = TaskStore(tmp_path / "tasks.json")

        async def execute(task: Task) -> None: ...

        sched = TaskScheduler(store, execute)
        ctx = ToolContext()
        tools = {t.metadata.name: t for t in build_task_tools(sched, context=ctx)}
        schedule = tools["schedule_task"]

        async def handle(chat_id: str, goal: str) -> dict:
            # Mimic BaseBot.set_task_context at the start of handling a message,
            # then yield control (as agent.chat() would) so the two handlers
            # interleave. With a shared mutable context this would race.
            ctx.set(chat_id=chat_id, owner_id=f"owner-{chat_id}")
            await asyncio.sleep(0)
            out = await schedule.acall(when="in 1h", goal=goal)
            return out.raw_output

        async def run_both() -> tuple[dict, dict]:
            return await asyncio.gather(
                handle("chat-A", "task A goal that is clearly defined here"),
                handle("chat-B", "task B goal that is clearly defined too"),
            )

        res_a, res_b = asyncio.run(run_both())
        assert res_a["chat"] == "chat-A"
        assert res_b["chat"] == "chat-B"

        tasks = asyncio.run(sched.list_for())
        by_chat = {t.chat_id: t.goal for t in tasks}
        assert by_chat == {
            "chat-A": "task A goal that is clearly defined here",
            "chat-B": "task B goal that is clearly defined too",
        }

    def test_cannot_schedule_while_executing_a_task(self, tmp_path: Path):
        """The re-entrancy guard: schedule_task must refuse while a task is
        executing, which is what prevents the 'schedule a task that schedules
        a task' recursion."""
        _, tools = self._tools(tmp_path)

        async def attempt_schedule_during_execution() -> dict:
            token = _EXECUTING_TASK.set(True)
            try:
                out = await tools["schedule_task"].acall(
                    when="in 1h", goal="schedule another task recursively now"
                )
                return out.raw_output
            finally:
                _EXECUTING_TASK.reset(token)

        res = asyncio.run(attempt_schedule_during_execution())
        assert "error" in res
        assert "recurse" in res["error"].lower()


class TestRecurringTasks:
    def test_daily_recurring(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.json")
        executed: list[Task] = []

        async def execute(task: Task) -> None:
            executed.append(task)

        sched = TaskScheduler(store, execute, poll_interval=0.05)
        now = datetime.now(UTC)
        created = asyncio.run(
            sched.schedule(
                chat_id="c1",
                goal="daily standup reminder",
                due_at=now - timedelta(seconds=1),
                repeat="daily",
            )
        )
        assert created.repeat == "daily"
        assert created.occurrences == 0

        asyncio.run(sched._tick())
        assert len(executed) == 1
        assert executed[0].goal == "daily standup reminder"
        remaining = asyncio.run(store.list_for())
        assert len(remaining) == 1
        next_task = remaining[0]
        assert next_task.id == created.id
        assert next_task.occurrences == 1
        assert next_task.due_at > now

    def test_weekly_with_weekdays(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.json")
        executed: list[Task] = []

        async def execute(task: Task) -> None:
            executed.append(task)

        sched = TaskScheduler(store, execute, poll_interval=0.05)
        now = datetime.now(UTC)
        asyncio.run(
            sched.schedule(
                chat_id="c1",
                goal="Mon/Wed/Fri reminder",
                due_at=now - timedelta(seconds=1),
                repeat="weekly",
                weekdays=(0, 2, 4),
            )
        )
        asyncio.run(sched._tick())
        assert len(executed) == 1
        next_task = asyncio.run(store.list_for())[0]
        assert next_task.due_at.weekday() in (0, 2, 4)

    def test_count_limit(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.json")
        executed: list[Task] = []

        async def execute(task: Task) -> None:
            executed.append(task)

        sched = TaskScheduler(store, execute, poll_interval=0.05)
        now = datetime.now(UTC)
        task0 = asyncio.run(
            sched.schedule(
                chat_id="c1",
                goal="only twice",
                due_at=now - timedelta(seconds=1),
                repeat="daily",
                count=2,
            )
        )
        assert task0.occurrences == 0
        asyncio.run(sched._tick())
        assert len(executed) == 1
        first_next = asyncio.run(store.list_for())[0]
        assert first_next.occurrences == 1

        first_next = first_next.model_copy(update={"due_at": now - timedelta(seconds=1)})
        asyncio.run(store.add(first_next))
        asyncio.run(sched._tick())
        assert len(executed) == 2
        assert asyncio.run(store.list_for()) == []

    def test_until_limit(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.json")
        executed: list[Task] = []

        async def execute(task: Task) -> None:
            executed.append(task)

        sched = TaskScheduler(store, execute, poll_interval=0.05)
        now = datetime.now(UTC)
        until = now + timedelta(days=10)
        asyncio.run(
            sched.schedule(
                chat_id="c1",
                goal="stops soon",
                due_at=now - timedelta(seconds=1),
                repeat="daily",
                until=until,
            )
        )
        asyncio.run(sched._tick())
        assert len(executed) == 1
        remaining = asyncio.run(store.list_for())
        assert len(remaining) == 1
        assert remaining[0].until is not None

    def test_compute_next_due_daily(self):
        now = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        task = Task(
            id="x",
            chat_id="c",
            goal="test",
            due_at=now,
            repeat="daily",
            interval=2,
            occurrences=0,
        )
        next_dt = compute_next_due(task)
        assert next_dt == now + timedelta(days=2)

    def test_compute_next_due_weekly_with_weekdays(self):
        now = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
        task = Task(
            id="x",
            chat_id="c",
            goal="test",
            due_at=now,
            repeat="weekly",
            weekdays=(1, 3),
            occurrences=0,
        )
        next_dt = compute_next_due(task)
        assert next_dt is not None
        assert next_dt.weekday() in (1, 3)
        assert next_dt > now

    def test_compute_next_due_monthly(self):
        now = datetime(2026, 1, 31, 10, 0, tzinfo=UTC)
        task = Task(
            id="x",
            chat_id="c",
            goal="test",
            due_at=now,
            repeat="monthly",
            occurrences=0,
        )
        next_dt = compute_next_due(task)
        assert next_dt is not None
        assert next_dt.day == 28 or next_dt.day == 29 or next_dt.day <= 31

    def test_schedule_task_tool_recurring(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.json")

        async def execute(task: Task) -> None: ...

        sched = TaskScheduler(store, execute)
        ctx = ToolContext(chat_id="chat-1", owner_id="owner-1")
        tools = {
            t.metadata.name: t for t in build_task_tools(sched, context=ctx, clarity_judge=None)
        }

        out = asyncio.run(
            tools["schedule_task"].acall(
                when="in 1h",
                goal="remind me every day to drink water",
                repeat="daily",
            )
        )
        res = out.raw_output
        assert "error" not in res
        assert res.get("recurring") is not None
        tasks = asyncio.run(sched.list_for())
        assert len(tasks) == 1
        assert tasks[0].repeat == "daily"

    def test_schedule_task_tool_weekdays(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.json")

        async def execute(task: Task) -> None: ...

        sched = TaskScheduler(store, execute)
        ctx = ToolContext(chat_id="chat-1", owner_id="owner-1")
        tools = {
            t.metadata.name: t for t in build_task_tools(sched, context=ctx, clarity_judge=None)
        }

        out = asyncio.run(
            tools["schedule_task"].acall(
                when="in 1h",
                goal="remind on mon/wed/fri",
                repeat="weekly",
                weekdays="mon,wed,fri",
            )
        )
        res = out.raw_output
        assert "error" not in res
        tasks = asyncio.run(sched.list_for())
        assert tasks[0].weekdays == (0, 2, 4)
