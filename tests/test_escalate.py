"""Tests for the escalate and blacklist tools.

These tools are default tools registered in get_tools() and rely on
module-level _DYN state set by Bot.configure(). The tests verify:
- escalate() persists (via EscalationStore) and fires callback
- blacklist() modifies the bot's blacklist
- _resolve_chat_id() works with explicit ID, ToolContext, and no context
- list_escalations() and get_active_escalations() filter correctly
- resolve_escalation() marks escalations as resolved
- EscalationStore persists to disk and reloads
- Tools are included in get_tools()
"""

from datetime import UTC, datetime

import httpx
import pytest
import respx

from kai.agent.context import ToolContext
from kai.agent.tools import get_tools
from kai.agent.tools.escalate import (
    _DYN,
    Escalation,
    EscalationStore,
    active_escalation_count,
    blacklist,
    clear_escalations,
    escalate,
    forward_to_cockpit,
    get_active_escalations,
    list_escalations,
    resolve_escalation,
    set_blacklist,
    set_cockpit_url,
    set_escalation_handler,
    set_escalation_secret,
    set_tool_context,
)


# Reset module state before each test (setup, not just teardown) so a
# file-based store left by another test file in the same xdist worker
# (e.g. cockpit tests calling create_app() → set_escalation_store(path))
# doesn't leak stale escalations into these tests.
@pytest.fixture(autouse=True)
def _reset_state():
    _reset_dyn_state()
    yield
    _reset_dyn_state()


def _reset_dyn_state() -> None:
    """Reset all module-level _DYN attributes to their defaults."""
    _DYN.on_escalation = None
    _DYN.blacklist = None
    _DYN.tool_context = None
    _DYN.cockpit_url = ""
    _DYN.cockpit_escalation_secret = ""
    _DYN.store = EscalationStore(None)


class TestEscalate:
    @pytest.mark.asyncio
    async def test_records_escalation(self):
        set_tool_context(ToolContext(chat_id="chat-1"))
        result = await escalate("Customer wants a human", severity="high")
        escalations = await list_escalations()
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
        set_tool_context(ToolContext(chat_id="chat-2"))
        await escalate("Something happened")
        esc = (await list_escalations())[0]
        assert esc.severity == "medium"

    @pytest.mark.asyncio
    async def test_fires_callback(self):
        received = []

        async def handler(esc):
            received.append(esc)

        set_escalation_handler(handler)
        set_tool_context(ToolContext(chat_id="chat-3"))
        await escalate("test escalation")
        assert len(received) == 1
        assert received[0].chat_id == "chat-3"

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_break_tool(self):
        async def bad_handler(esc):
            raise RuntimeError("bad handler")

        set_escalation_handler(bad_handler)
        set_tool_context(ToolContext(chat_id="chat-4"))
        result = await escalate("still works")
        assert "still works" in result

    @pytest.mark.asyncio
    async def test_escalation_records_empty_chat_id_when_context_missing(self):
        set_tool_context(ToolContext())
        await escalate("no ctx")
        esc = (await list_escalations())[0]
        assert esc.chat_id == ""

    @pytest.mark.asyncio
    async def test_severity_critical(self):
        set_tool_context(ToolContext(chat_id="chat-5"))
        await escalate("Critical issue", severity="critical")
        esc = (await list_escalations())[0]
        assert esc.severity == "critical"

    @pytest.mark.asyncio
    async def test_contains_summary(self):
        set_tool_context(ToolContext(chat_id="chat-6"))
        await escalate("test reason", severity="low", summary="test summary")
        esc = (await list_escalations())[0]
        assert esc.summary == "test summary"
        assert esc.severity == "low"

    @pytest.mark.asyncio
    async def test_created_at_is_set(self):
        set_tool_context(ToolContext(chat_id="chat-7"))
        before = datetime.now(UTC)
        await escalate("test")
        esc = (await list_escalations())[0]
        after = datetime.now(UTC)
        assert before <= esc.created_at <= after

    @pytest.mark.asyncio
    async def test_id_is_unique(self):
        set_tool_context(ToolContext(chat_id="chat-8"))
        await escalate("first")
        await escalate("second")
        escs = await list_escalations()
        assert escs[0].id != escs[1].id


class TestBlacklistContact:
    @pytest.mark.asyncio
    async def test_adds_contact_to_blacklist(self):
        bl = ["existing@example.com"]
        set_blacklist(bl)
        set_tool_context(ToolContext(chat_id="new@example.com"))
        result = await blacklist()
        assert "new@example.com" in bl
        assert "blacklisted" in result
        assert "new@example.com" in result

    @pytest.mark.asyncio
    async def test_explicit_contact_id(self):
        bl = []
        set_blacklist(bl)
        result = await blacklist("someone@example.com")
        assert "someone@example.com" in bl
        assert "someone@example.com" in result

    @pytest.mark.asyncio
    async def test_no_duplicate_blacklist_entry(self):
        bl = ["duplicated@example.com"]
        set_blacklist(bl)
        set_tool_context(ToolContext(chat_id="duplicated@example.com"))
        await blacklist()
        assert bl.count("duplicated@example.com") == 1

    @pytest.mark.asyncio
    async def test_error_when_no_blacklist(self):
        result = await blacklist("test@example.com")
        assert "Error" in result
        assert "not configured" in result

    @pytest.mark.asyncio
    async def test_error_when_no_chat_id(self):
        bl = []
        set_blacklist(bl)
        set_tool_context(ToolContext())
        result = await blacklist()
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_falls_back_to_tool_context(self):
        bl = []
        set_blacklist(bl)
        set_tool_context(ToolContext(chat_id="ctx-chat"))
        await blacklist()
        assert "ctx-chat" in bl

    @pytest.mark.asyncio
    async def test_blacklist_persists_across_calls(self):
        bl = ["alpha@example.com"]
        set_blacklist(bl)
        set_tool_context(ToolContext(chat_id="beta@example.com"))
        await blacklist()
        assert "alpha@example.com" in bl
        assert "beta@example.com" in bl

    @pytest.mark.asyncio
    async def test_blacklist_creates_escalation_record(self):
        bl = []
        set_blacklist(bl)
        set_tool_context(ToolContext(chat_id="spammer@example.com"))
        await blacklist()
        escs = await list_escalations()
        blacklist_esc = [e for e in escs if "blacklisted" in e.reason]
        assert len(blacklist_esc) == 1
        assert blacklist_esc[0].chat_id == "spammer@example.com"

    @pytest.mark.asyncio
    async def test_blacklist_fires_escalation_handler(self):
        received = []

        async def handler(esc):
            received.append(esc)

        set_escalation_handler(handler)
        bl = []
        set_blacklist(bl)
        set_tool_context(ToolContext(chat_id="handler-test@example.com"))
        await blacklist()
        assert len(received) == 1
        assert received[0].chat_id == "handler-test@example.com"
        assert "blacklisted" in received[0].reason


class TestResolveChatId:
    @pytest.mark.asyncio
    async def test_tool_context_provides_chat_id(self):
        set_tool_context(ToolContext(chat_id="from-context"))
        set_blacklist([])
        await blacklist()
        escs = await list_escalations()
        assert any(e.chat_id == "from-context" for e in escs)

    @pytest.mark.asyncio
    async def test_explicit_contact_id_refused_when_not_current_chat(self):
        # Prompt-injection guard: an explicit contact_id that doesn't match the
        # current chat is refused — the model can only blacklist the contact
        # it's currently talking to.
        set_tool_context(ToolContext(chat_id="from-context"))
        set_blacklist([])
        result = await blacklist("explicit-contact")
        assert "Error" in result
        assert "from-context" in result
        escs = await list_escalations()
        assert not any(e.chat_id == "explicit-contact" for e in escs)

    @pytest.mark.asyncio
    async def test_explicit_contact_id_matching_current_chat_allowed(self):
        # An explicit contact_id that matches the current chat is redundant
        # but allowed.
        set_tool_context(ToolContext(chat_id="same@example.com"))
        bl = []
        set_blacklist(bl)
        result = await blacklist("same@example.com")
        assert "same@example.com" in bl
        assert "blacklisted" in result


class TestInspectionHelpers:
    @pytest.mark.asyncio
    async def test_list_escalations_empty_initially(self):
        assert await list_escalations() == []

    @pytest.mark.asyncio
    async def test_get_active_escalations_empty_initially(self):
        assert await get_active_escalations() == []

    @pytest.mark.asyncio
    async def test_resolve_nonexistent_escalation(self):
        assert await resolve_escalation("nonexistent") is False

    @pytest.mark.asyncio
    async def test_resolve_escalation_marks_as_resolved(self):
        set_tool_context(ToolContext(chat_id="test-chat"))
        await escalate("test reason")
        escalations = await list_escalations()
        assert len(escalations) == 1
        esc_id = escalations[0].id
        assert await resolve_escalation(esc_id) is True
        resolved_esc = (await list_escalations())[0]
        assert resolved_esc.resolved is True
        assert resolved_esc.resolved_at is not None
        active = await get_active_escalations()
        assert len(active) == 0
        assert await resolve_escalation(esc_id) is False

    @pytest.mark.asyncio
    async def test_resolve_escalation_records_resolved_by(self):
        set_tool_context(ToolContext(chat_id="test-chat"))
        await escalate("test reason")
        esc_id = (await list_escalations())[0].id
        assert await resolve_escalation(esc_id, resolved_by="operator@example.com") is True
        resolved_esc = (await list_escalations())[0]
        assert resolved_esc.resolved_by == "operator@example.com"

    @pytest.mark.asyncio
    async def test_clear_escalations_resets_all(self):
        set_tool_context(ToolContext(chat_id="test"))
        await escalate("one")
        await escalate("two")
        assert len(await list_escalations()) == 2
        await clear_escalations()
        assert len(await list_escalations()) == 0

    @pytest.mark.asyncio
    async def test_active_vs_all_escalations(self):
        set_tool_context(ToolContext(chat_id="test"))
        await escalate("unresolved 1")
        await escalate("unresolved 2")
        await escalate("resolved one")
        escs = await list_escalations()
        assert len(escs) == 3
        await resolve_escalation(escs[2].id)
        assert len(await list_escalations()) == 3
        assert len(await get_active_escalations()) == 2

    @pytest.mark.asyncio
    async def test_resolve_sets_resolved_at_and_preserves_resolved(self):
        set_tool_context(ToolContext(chat_id="test"))
        await escalate("test")
        esc = (await list_escalations())[0]
        assert esc.resolved_at is None
        assert esc.resolved_by is None
        await resolve_escalation(esc.id)
        resolved_esc = (await list_escalations())[0]
        assert resolved_esc.resolved_at is not None
        assert resolved_esc.resolved is True


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
    async def test_list_for_chat_filters(self, tmp_path):
        store = EscalationStore(None)
        await store.add(Escalation(id="esc-1", chat_id="chat-a", reason="a"))
        await store.add(Escalation(id="esc-2", chat_id="chat-b", reason="b"))
        escs = await store.list_for_chat("chat-a")
        assert len(escs) == 1
        assert escs[0].id == "esc-1"

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
        tools = get_tools()
        names = [t.metadata.name for t in tools]
        assert "escalate" in names, "escalate should be in default tools"

    def test_blacklist_in_default_tools(self):
        tools = get_tools()
        names = [t.metadata.name for t in tools]
        assert "blacklist" in names, "blacklist must be in default tools"

    def test_original_default_tools_still_present(self):
        tools = get_tools()
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
        tools = get_tools()
        # 5 original + escalate + blacklist = 7
        assert len(tools) == 7, f"Expected 7 tools, got {len(tools)}"


class TestSetCockpitUrl:
    def test_empty_url_disables_forwarding(self):
        set_cockpit_url("")
        assert _DYN.cockpit_url == ""

    def test_strips_trailing_slash(self):
        set_cockpit_url("http://cockpit:8080/")
        assert _DYN.cockpit_url == "http://cockpit:8080"

    def test_keeps_url_without_trailing_slash(self):
        set_cockpit_url("http://cockpit:8080")
        assert _DYN.cockpit_url == "http://cockpit:8080"


class TestForwardToCockpit:
    @pytest.mark.asyncio
    async def test_noop_when_cockpit_url_empty(self):
        # No cockpit_url set → forward_to_cockpit returns without raising.
        set_cockpit_url("")
        esc = Escalation(id="esc-1", chat_id="c", reason="test")
        await forward_to_cockpit(esc)  # must not raise

    @respx.mock
    @pytest.mark.asyncio
    async def test_posts_escalation_to_cockpit(self):
        set_cockpit_url("http://cockpit:8080")
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
        await forward_to_cockpit(esc)
        assert route.called
        import json

        body = json.loads(route.calls.last.request.content)
        assert body["id"] == "esc-1"
        assert body["reason"] == "Customer wants a human"
        assert body["severity"] == "high"

    @respx.mock
    @pytest.mark.asyncio
    async def test_does_not_raise_on_http_error(self):
        set_cockpit_url("http://cockpit:8080")
        respx.post("http://cockpit:8080/api/escalations").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        esc = Escalation(id="esc-1", chat_id="c", reason="test")
        # Must not raise — forwarding is best-effort.
        await forward_to_cockpit(esc)

    @respx.mock
    @pytest.mark.asyncio
    async def test_does_not_raise_on_4xx_response(self):
        set_cockpit_url("http://cockpit:8080")
        respx.post("http://cockpit:8080/api/escalations").mock(
            return_value=httpx.Response(400, text="bad request")
        )
        esc = Escalation(id="esc-1", chat_id="c", reason="test")
        await forward_to_cockpit(esc)  # must not raise

    @respx.mock
    @pytest.mark.asyncio
    async def test_sends_bearer_token_when_secret_set(self):
        set_cockpit_url("http://cockpit:8080")
        set_escalation_secret("s3cret")
        route = respx.post("http://cockpit:8080/api/escalations").mock(
            return_value=httpx.Response(201, json={"ok": True})
        )
        esc = Escalation(id="esc-1", chat_id="c", reason="test")
        await forward_to_cockpit(esc)
        assert route.called
        assert route.calls.last.request.headers["Authorization"] == "Bearer s3cret"

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_auth_header_when_secret_unset(self):
        set_cockpit_url("http://cockpit:8080")
        # _reset_state sets _DYN.cockpit_escalation_secret = "" (verify below)
        route = respx.post("http://cockpit:8080/api/escalations").mock(
            return_value=httpx.Response(201, json={"ok": True})
        )
        esc = Escalation(id="esc-1", chat_id="c", reason="test")
        await forward_to_cockpit(esc)
        assert route.called
        assert "Authorization" not in route.calls.last.request.headers

    @respx.mock
    @pytest.mark.asyncio
    async def test_logs_warning_on_401(self, caplog):
        set_cockpit_url("http://cockpit:8080")
        set_escalation_secret("s3cret")
        respx.post("http://cockpit:8080/api/escalations").mock(
            return_value=httpx.Response(401, text="unauthorized")
        )
        esc = Escalation(id="esc-1", chat_id="c", reason="test")
        with caplog.at_level("WARNING", logger="kai.agent.tools.escalate"):
            await forward_to_cockpit(esc)
        assert any("bad secret" in r.message for r in caplog.records)


class TestActiveCount:
    def test_active_count_zero_initially(self):
        store = EscalationStore(None)
        assert store.active_count() == 0

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

    def test_active_escalation_count_reads_current_store(self):
        # Module-level helper reads _DYN.store (the default in-memory store
        # from the _reset_state fixture).
        assert active_escalation_count() == 0


class TestBaseBotOnEscalation:
    """The default BaseBot.on_escalation logs and forwards to the cockpit."""

    @pytest.mark.asyncio
    async def test_forwards_to_cockpit(self, monkeypatch):
        from pathlib import Path

        from kai.bots.base import BaseBot

        forwarded: list = []
        # forward_to_cockpit is imported at module level into base.py, so
        # patch it there (where on_escalation looks it up).
        monkeypatch.setattr(
            "kai.bots.base.forward_to_cockpit",
            lambda esc: (forwarded.append(esc), _async_noop())[-1],
        )

        class _ConcreteBot(BaseBot):
            async def run(self) -> None:
                pass

        bot = _ConcreteBot(Path("/tmp"))
        esc = Escalation(id="e1", chat_id="c", reason="r", severity="critical")
        await bot.on_escalation(esc)
        assert len(forwarded) == 1
        assert forwarded[0].id == "e1"


async def _async_noop() -> None:
    return None
