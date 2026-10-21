"""Tests for the escalate and blacklist tools.

The tools are bound methods of :class:`EscalationState` registered in
``get_tools()``; each bot wires its own state in ``Bot.configure()``. The
tests verify:
- escalate() persists (via EscalationStore) and fires callback
- add_to_blacklist() modifies the bot's blacklist
- resolve_chat_id() works with explicit ID, ToolContext, and no context
- store listing/resolve helpers filter correctly
- EscalationStore persists to disk and reloads
- Tools are included in get_tools()
- cockpit forwarding (including non-2xx responses)
"""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from kai.agent.context import ToolContext
from kai.agent.tools import get_tools
from kai.agent.tools.escalate import Escalation, EscalationState, EscalationStore


def make_state(
    chat_id: str | None = None,
    **kwargs,
) -> EscalationState:
    """A fresh escalation state, optionally bound to a chat context."""
    ctx = ToolContext(chat_id=chat_id) if chat_id is not None else None
    return EscalationState(tool_context=ctx, **kwargs)


class TestEscalate:
    @pytest.mark.asyncio
    async def test_escalate_persists_escalation_record(self):
        state = make_state(chat_id="chat-1")
        result = await state.escalate("Customer wants a human", severity="high")
        escalations = await state.store.list_all()
        assert len(escalations) == 1
        esc = escalations[0]
        assert esc.chat_id == "chat-1"
        assert esc.conversation_id == "chat-1"
        assert esc.reason == "Customer wants a human"
        assert esc.severity == "high"
        assert not esc.resolved
        assert "escalation recorded" in result
        assert "high" in result

    @pytest.mark.asyncio
    async def test_uses_default_severity(self):
        state = make_state(chat_id="chat-2")
        await state.escalate("Something happened")
        esc = (await state.store.list_all())[0]
        assert esc.severity == "medium"

    @pytest.mark.asyncio
    async def test_escalate_fires_callback(self):
        received = []

        async def handler(esc):
            received.append(esc)

        state = make_state(chat_id="chat-3", on_escalation=handler)
        await state.escalate("test escalation")
        assert len(received) == 1
        assert received[0].chat_id == "chat-3"

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_break_tool(self):
        async def bad_handler(esc):
            raise RuntimeError("bad handler")

        state = make_state(chat_id="chat-4", on_escalation=bad_handler)
        result = await state.escalate("still works")
        assert "still works" in result

    @pytest.mark.asyncio
    async def test_escalation_records_empty_chat_id_when_context_missing(self):
        state = make_state()
        await state.escalate("no ctx")
        esc = (await state.store.list_all())[0]
        assert esc.chat_id == ""

    @pytest.mark.asyncio
    async def test_escalate_accepts_critical_severity(self):
        state = make_state(chat_id="chat-5")
        await state.escalate("Critical issue", severity="critical")
        esc = (await state.store.list_all())[0]
        assert esc.severity == "critical"

    @pytest.mark.asyncio
    async def test_escalate_records_summary(self):
        state = make_state(chat_id="chat-6")
        await state.escalate("test reason", severity="low", summary="test summary")
        esc = (await state.store.list_all())[0]
        assert esc.summary == "test summary"
        assert esc.severity == "low"

    @pytest.mark.asyncio
    async def test_created_at_is_set(self):
        state = make_state(chat_id="chat-7")
        before = datetime.now(UTC)
        await state.escalate("test")
        esc = (await state.store.list_all())[0]
        after = datetime.now(UTC)
        assert before <= esc.created_at <= after

    @pytest.mark.asyncio
    async def test_id_is_unique(self):
        state = make_state(chat_id="chat-8")
        await state.escalate("first")
        await state.escalate("second")
        escs = await state.store.list_all()
        assert escs[0].id != escs[1].id

    @pytest.mark.asyncio
    async def test_owner_slug_stamped_on_escalations(self):
        state = make_state(chat_id="chat-9", owner_slug="david")
        await state.escalate("owned")
        esc = (await state.store.list_all())[0]
        assert esc.user_id == "david"


class TestBlacklistContact:
    @pytest.mark.asyncio
    async def test_adds_contact_to_blacklist(self):
        bl = ["existing@example.com"]
        state = make_state(chat_id="new@example.com", blacklist=bl)
        result = await state.add_to_blacklist()
        assert "new@example.com" in bl
        assert "blacklisted" in result
        assert "new@example.com" in result

    @pytest.mark.asyncio
    async def test_explicit_contact_id(self):
        bl = []
        state = make_state(blacklist=bl)
        result = await state.add_to_blacklist("someone@example.com")
        assert "someone@example.com" in bl
        assert "someone@example.com" in result

    @pytest.mark.asyncio
    async def test_no_duplicate_blacklist_entry(self):
        bl = ["duplicated@example.com"]
        state = make_state(chat_id="duplicated@example.com", blacklist=bl)
        await state.add_to_blacklist()
        assert bl.count("duplicated@example.com") == 1

    @pytest.mark.asyncio
    async def test_error_when_no_blacklist(self):
        state = make_state()
        result = await state.add_to_blacklist("test@example.com")
        assert "Error" in result
        assert "not configured" in result

    @pytest.mark.asyncio
    async def test_error_when_no_chat_id(self):
        state = make_state(blacklist=[])
        result = await state.add_to_blacklist()
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_falls_back_to_tool_context(self):
        bl = []
        state = make_state(chat_id="ctx-chat", blacklist=bl)
        await state.add_to_blacklist()
        assert "ctx-chat" in bl

    @pytest.mark.asyncio
    async def test_blacklist_persists_across_calls(self):
        bl = ["alpha@example.com"]
        state = make_state(chat_id="beta@example.com", blacklist=bl)
        await state.add_to_blacklist()
        assert "alpha@example.com" in bl
        assert "beta@example.com" in bl

    @pytest.mark.asyncio
    async def test_blacklist_creates_escalation_record(self):
        state = make_state(chat_id="spammer@example.com", blacklist=[])
        await state.add_to_blacklist()
        escs = await state.store.list_all()
        blacklist_esc = [e for e in escs if "blacklisted" in e.reason]
        assert len(blacklist_esc) == 1
        assert blacklist_esc[0].chat_id == "spammer@example.com"

    @pytest.mark.asyncio
    async def test_blacklist_fires_escalation_handler(self):
        received = []

        async def handler(esc):
            received.append(esc)

        state = make_state(
            chat_id="handler-test@example.com",
            blacklist=[],
            on_escalation=handler,
        )
        await state.add_to_blacklist()
        assert len(received) == 1
        assert received[0].chat_id == "handler-test@example.com"
        assert "blacklisted" in received[0].reason


class TestResolveChatId:
    def test_explicit_override_wins(self):
        state = make_state(chat_id="from-context")
        assert state.resolve_chat_id("explicit") == "explicit"

    def test_tool_context_provides_chat_id(self):
        state = make_state(chat_id="from-context")
        assert state.resolve_chat_id() == "from-context"

    def test_empty_when_no_context(self):
        assert EscalationState().resolve_chat_id() == ""

    @pytest.mark.asyncio
    async def test_explicit_contact_id_refused_when_not_current_chat(self):
        # Prompt-injection guard: an explicit contact_id that doesn't match the
        # current chat is refused — the model can only blacklist the contact
        # it's currently talking to.
        state = make_state(chat_id="from-context", blacklist=[])
        result = await state.add_to_blacklist("explicit-contact")
        assert "Error" in result
        assert "from-context" in result
        assert not any(e.chat_id == "explicit-contact" for e in await state.store.list_all())

    @pytest.mark.asyncio
    async def test_explicit_contact_id_matching_current_chat_allowed(self):
        # An explicit contact_id that matches the current chat is redundant
        # but allowed.
        state = make_state(chat_id="same@example.com", blacklist=[])
        result = await state.add_to_blacklist("same@example.com")
        assert state.blacklist is not None
        assert "same@example.com" in state.blacklist
        assert "blacklisted" in result


class TestEscalationStoreQueries:
    @pytest.mark.asyncio
    async def test_list_all_empty_initially(self):
        assert await EscalationState().store.list_all() == []

    @pytest.mark.asyncio
    async def test_list_active_empty_initially(self):
        assert await EscalationState().store.list_active() == []

    @pytest.mark.asyncio
    async def test_resolve_nonexistent_escalation(self):
        assert await EscalationState().store.resolve("nonexistent") is False

    @pytest.mark.asyncio
    async def test_resolve_escalation_marks_as_resolved(self):
        state = make_state(chat_id="test-chat")
        await state.escalate("test reason")
        escalations = await state.store.list_all()
        assert len(escalations) == 1
        esc_id = escalations[0].id
        assert await state.store.resolve(esc_id) is True
        resolved_esc = (await state.store.list_all())[0]
        assert resolved_esc.resolved is True
        assert resolved_esc.resolved_at is not None
        assert len(await state.store.list_active()) == 0
        assert await state.store.resolve(esc_id) is False

    @pytest.mark.asyncio
    async def test_resolve_escalation_records_resolved_by(self):
        state = make_state(chat_id="test-chat")
        await state.escalate("test reason")
        esc_id = (await state.store.list_all())[0].id
        ok = await state.store.resolve(esc_id, resolved_by="operator@example.com")
        assert ok is True
        resolved_esc = (await state.store.list_all())[0]
        assert resolved_esc.resolved_by == "operator@example.com"

    @pytest.mark.asyncio
    async def test_active_vs_all_escalations(self):
        state = make_state(chat_id="test")
        await state.escalate("unresolved 1")
        await state.escalate("unresolved 2")
        await state.escalate("resolved one")
        escs = await state.store.list_all()
        assert len(escs) == 3
        await state.store.resolve(escs[2].id)
        assert len(await state.store.list_all()) == 3
        assert len(await state.store.list_active()) == 2


class TestEscalationStorePersistence:
    @pytest.mark.asyncio
    async def test_in_memory_store_returns_added_item(self, tmp_path):
        store = EscalationStore(None)
        esc = Escalation(id="esc-1", chat_id="chat-1", reason="test")
        await store.add(esc)
        assert await store.list_all() == [esc]

    @pytest.mark.asyncio
    async def test_persists_across_store_instances(self, tmp_path):
        path = tmp_path / "escalations.json"
        store = EscalationStore(path)
        esc = Escalation(id="esc-1", chat_id="chat-1", reason="test", severity="high")
        await store.add(esc)
        assert path.exists()

        reloaded = EscalationStore(path)
        escs = await reloaded.list_all()
        assert len(escs) == 1
        assert escs[0].id == "esc-1"
        assert escs[0].chat_id == "chat-1"
        assert escs[0].severity == "high"

    @pytest.mark.asyncio
    async def test_resolve_persists_across_instances(self, tmp_path):
        path = tmp_path / "escalations.json"
        store = EscalationStore(path)
        esc = Escalation(id="esc-1", chat_id="chat-1", reason="test")
        await store.add(esc)
        assert await store.resolve("esc-1", resolved_by="ops@example.com") is True

        reloaded = EscalationStore(path)
        escs = await reloaded.list_all()
        assert escs[0].resolved is True
        assert escs[0].resolved_by == "ops@example.com"

    @pytest.mark.asyncio
    async def test_list_for_user_excludes_unowned(self, tmp_path):
        store = EscalationStore(None)
        await store.add(Escalation(id="esc-1", chat_id="chat-a", reason="a", user_id="david"))
        await store.add(Escalation(id="esc-2", chat_id="chat-b", reason="b"))
        escs = await store.list_for_user("david")
        assert [e.id for e in escs] == ["esc-1"]

    @pytest.mark.asyncio
    async def test_missing_file_starts_empty(self, tmp_path):
        path = tmp_path / "does-not-exist.json"
        store = EscalationStore(path)
        assert await store.list_all() == []

    @pytest.mark.asyncio
    async def test_corrupt_file_does_not_crash(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("not json", encoding="utf-8")
        store = EscalationStore(path)
        assert await store.list_all() == []


class TestDefaultToolsIncludeNewTools:
    def test_escalate_in_default_tools(self):
        tools = get_tools(EscalationState())
        names = [t.metadata.name for t in tools]
        assert "escalate" in names, "escalate should be in default tools"

    def test_blacklist_in_default_tools(self):
        tools = get_tools(EscalationState())
        names = [t.metadata.name for t in tools]
        assert "blacklist" in names, "blacklist must be in default tools"

    def test_original_default_tools_still_present(self):
        tools = get_tools(EscalationState())
        names = [t.metadata.name for t in tools]
        for expected in (
            "web_search",
            "get_webpage_content",
            "get_time_in_timezone",
            "get_weather",
            "calculate",
        ):
            assert expected in names, f"{expected} should still be in default tools"

    def test_tool_count_includes_new_tools(self):
        tools = get_tools(EscalationState())
        # 5 original + escalate + blacklist = 7
        assert len(tools) == 7, f"Expected 7 tools, got {len(tools)}"


class TestCockpitUrlNormalization:
    def test_empty_url_disables_forwarding(self):
        assert EscalationState(cockpit_url="").cockpit_url == ""

    def test_strips_trailing_slash(self):
        assert EscalationState(cockpit_url="http://cockpit:8080/").cockpit_url == (
            "http://cockpit:8080"
        )

    def test_keeps_url_without_trailing_slash(self):
        assert EscalationState(cockpit_url="http://cockpit:8080").cockpit_url == (
            "http://cockpit:8080"
        )


class TestForwardToCockpit:
    @respx.mock
    @pytest.mark.asyncio
    async def test_noop_when_cockpit_url_empty(self):
        state = EscalationState(cockpit_url="")
        route = respx.post("http://cockpit:8080/api/escalations")
        esc = Escalation(id="esc-1", chat_id="c", reason="test")

        await state.forward_to_cockpit(esc)

        assert not route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_posts_escalation_to_cockpit(self):
        state = EscalationState(cockpit_url="http://cockpit:8080")
        route = respx.post("http://cockpit:8080/api/escalations").mock(
            return_value=httpx.Response(201, json={"ok": True})
        )
        esc = Escalation(
            id="esc-1",
            chat_id="chat-99",
            reason="Customer wants a human",
            severity="high",
            summary="context",
        )
        await state.forward_to_cockpit(esc)
        assert route.called
        import json

        body = json.loads(route.calls.last.request.content)
        assert body["id"] == "esc-1"
        assert body["reason"] == "Customer wants a human"
        assert body["severity"] == "high"

    @respx.mock
    @pytest.mark.asyncio
    async def test_does_not_raise_on_http_error(self):
        state = EscalationState(cockpit_url="http://cockpit:8080")
        route = respx.post("http://cockpit:8080/api/escalations").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        esc = Escalation(id="esc-1", chat_id="c", reason="test")

        await state.forward_to_cockpit(esc)

        assert route.called

    @respx.mock
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [400, 401])
    async def test_does_not_raise_on_4xx_response(self, status):
        state = EscalationState(cockpit_url="http://cockpit:8080")
        route = respx.post("http://cockpit:8080/api/escalations").mock(
            return_value=httpx.Response(status, text="rejected")
        )
        esc = Escalation(id="esc-1", chat_id="c", reason="test")

        await state.forward_to_cockpit(esc)

        assert route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_sends_bearer_token_when_secret_set(self):
        state = EscalationState(
            cockpit_url="http://cockpit:8080", cockpit_escalation_secret="s3cret"
        )
        route = respx.post("http://cockpit:8080/api/escalations").mock(
            return_value=httpx.Response(201, json={"ok": True})
        )
        esc = Escalation(id="esc-1", chat_id="c", reason="test")
        await state.forward_to_cockpit(esc)
        assert route.called
        assert route.calls.last.request.headers["Authorization"] == "Bearer s3cret"

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_auth_header_when_secret_unset(self):
        state = EscalationState(cockpit_url="http://cockpit:8080")
        route = respx.post("http://cockpit:8080/api/escalations").mock(
            return_value=httpx.Response(201, json={"ok": True})
        )
        esc = Escalation(id="esc-1", chat_id="c", reason="test")
        await state.forward_to_cockpit(esc)
        assert route.called
        assert "Authorization" not in route.calls.last.request.headers


class TestActiveCount:
    def test_active_count_zero_initially(self):
        assert EscalationState().store.active_count() == 0

    @pytest.mark.asyncio
    async def test_active_count_counts_unresolved(self):
        store = EscalationStore(None)
        await store.add(Escalation(id="e1", chat_id="c", reason="a"))
        await store.add(Escalation(id="e2", chat_id="c", reason="b"))
        assert store.active_count() == 2

    @pytest.mark.asyncio
    async def test_active_count_excludes_resolved(self):
        store = EscalationStore(None)
        await store.add(Escalation(id="e1", chat_id="c", reason="a"))
        await store.add(Escalation(id="e2", chat_id="c", reason="b"))
        await store.resolve("e1")
        assert store.active_count() == 1


class TestBaseBotOnEscalation:
    """The default BaseBot.on_escalation logs and forwards via its state."""

    @pytest.mark.asyncio
    async def test_forwards_to_cockpit(self):
        from kai.bots.base import BaseBot

        class _ConcreteBot(BaseBot):
            async def run(self) -> None:
                pass

        bot = _ConcreteBot(Path("/tmp"))
        forwarded: list = []

        async def fake_forward(esc):
            forwarded.append(esc)

        bot.escalations.forward_to_cockpit = fake_forward  # type: ignore[method-assign]
        esc = Escalation(id="e1", chat_id="c", reason="r", severity="critical")
        await bot.on_escalation(esc)
        assert len(forwarded) == 1
        assert forwarded[0].id == "e1"
