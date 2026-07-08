import hashlib
import hmac as hmac_mod
import json
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from kai.agent.core import ChatResult, KaiAgent, ToolCallRecord, _action_values
from kai.bots.base import TellResult
from kai.bots.waha import Bot, BotConfig
from kai.bots.waha.actions import (
    _FULL_ACTIONS,
    WahaAction,
    WahaNoSilentAction,
    WahaNoVoiceAction,
)
from kai.bots.waha.webhook import create_webhook_app
from kai.runs import RunRecord, RunRegistry, generate_run_id, pid_alive, runs_path

_KEY = "test-secret"


def _make_bot(config: BotConfig | None = None, bot_dir: Path | None = None) -> Bot:
    bot = Bot(bot_dir=bot_dir or Path("."), config=config or BotConfig())
    return bot


def _operator_result(
    reply: str,
    *,
    action: str = "console",
    target: str | None = None,
    tool_calls=None,
    error=None,
) -> ChatResult:
    return ChatResult(
        reply=reply,
        tool_calls=tool_calls or [],
        action=WahaAction(action=cast(_FULL_ACTIONS, action), text=reply, target=target),
        error=error,
    )


class TestTellEndpoints:
    def test_tell_endpoint_normalizes_wildcard(self):
        bot = _make_bot()
        bot._waha = MagicMock()
        bot._waha.webhook_host = "0.0.0.0"
        bot._waha.webhook_port = 8000
        assert bot.tell_endpoint() == "http://127.0.0.1:8000"

    def test_tell_endpoint_keeps_loopback(self):
        bot = _make_bot()
        bot._waha = MagicMock()
        bot._waha.webhook_host = "127.0.0.1"
        bot._waha.webhook_port = 9000
        assert bot.tell_endpoint() == "http://127.0.0.1:9000"

    def test_tell_endpoint_none_when_unconfigured(self):
        assert _make_bot().tell_endpoint() is None

    def test_tell_hmac_key_passes_through(self):
        bot = _make_bot()
        bot._waha = MagicMock()
        bot._waha.hmac_key = "sekret"
        bot._waha.hmac_algorithm = "sha256"
        assert bot.tell_hmac_key() == "sekret"
        assert bot.tell_hmac_algorithm() == "sha256"


class TestExtractChatId:
    def test_finds_group_jid(self):
        assert Bot._extract_chat_id("send to 120363@g.us now") == "120363@g.us"

    def test_finds_dm_jid(self):
        assert Bot._extract_chat_id("message 18091234567@c.us please") == "18091234567@c.us"

    def test_returns_none_when_absent(self):
        assert Bot._extract_chat_id("send a joke to the family group") is None


class TestActionAsTool:
    """The model sometimes emits an action value (e.g. ``send_to_group``) as a
    tool call. The agent should recognize that and resolve it into the typed
    action rather than recording a failed "unknown tool" dispatch.
    """

    def test_action_values_extracts_enum(self):
        values = _action_values(WahaAction)
        assert "send_to_group" in values
        assert "console" in values
        assert "silent" in values

    def test_action_values_omits_silent_for_no_silent_cls(self):
        values = _action_values(WahaNoSilentAction)
        assert "silent" not in values
        assert "send_to_group" in values

    def test_action_from_tool_call_builds_send_to_group(self):
        action = KaiAgent._action_from_tool_call(
            WahaAction,
            "send_to_group",
            {"target": "11235677890-1111111111@g.us", "text": "hi there"},
        )
        assert isinstance(action, WahaAction)
        assert action.action == "send_to_group"
        assert action.target == "11235677890-1111111111@g.us"
        assert action.text == "hi there"

    def test_action_from_tool_call_returns_none_on_invalid(self):
        # A non-coercible ``text`` (dict instead of str) fails validation and
        # yields None rather than raising.
        action = KaiAgent._action_from_tool_call(
            WahaAction, "send_to_group", {"text": {"bad": "type"}}
        )
        assert action is None

    def test_action_from_tool_call_rejects_unknown_action(self):
        action = KaiAgent._action_from_tool_call(WahaAction, "not_a_real_action", {"text": "x"})
        assert action is None


class TestBuildTellResult:
    def test_actions_from_tool_calls(self):
        bot = _make_bot()
        result = _operator_result(
            "done",
            tool_calls=[
                ToolCallRecord(
                    name="set_goal",
                    args={"goal": "be concise"},
                    ok=True,
                    result="goal set",
                )
            ],
        )
        env = bot._build_tell_result(result, persist=False)
        assert env.ok is True
        assert env.reply == "done"
        assert env.actions == [{"tool": "set_goal", "goal": "be concise", "ok": True}]

    def test_error_marks_not_ok(self):
        bot = _make_bot()
        result = _operator_result("", error="schema fail")
        env = bot._build_tell_result(result, persist=False)
        assert env.ok is False
        assert "schema fail" in env.reply

    def test_truncates_long_text_param(self):
        bot = _make_bot()
        long = "x" * 500
        result = _operator_result(
            "ok",
            tool_calls=[
                ToolCallRecord(name="set_goal", args={"goal": long}, ok=True, result=""),
            ],
        )
        env = bot._build_tell_result(result, persist=False)
        assert env.actions[0]["goal"].endswith("...")
        assert len(env.actions[0]["goal"]) == 200


class TestOperatorTools:
    def test_no_send_message_tool(self):
        bot = _make_bot()
        bot._agent = MagicMock()
        bot._agent.get_tools.return_value = []
        tools = bot._operator_tools(persist=False)
        names = [t.metadata.name for t in tools]
        assert "send_message" not in names
        assert "set_goal" not in names

    def test_set_goal_only_when_persist(self):
        bot = _make_bot()
        bot._agent = MagicMock()
        bot._agent.get_tools.return_value = []
        tools = bot._operator_tools(persist=True)
        names = [t.metadata.name for t in tools]
        assert "send_message" not in names
        assert "set_goal" in names

    def test_includes_get_chat_history_when_registered(self):
        bot = _make_bot()
        hist = MagicMock()
        hist.metadata.name = "get_chat_history"
        bot._agent = MagicMock()
        bot._agent.get_tools.return_value = [hist]
        tools = bot._operator_tools(persist=False)
        names = [t.metadata.name for t in tools]
        assert "get_chat_history" in names


class TestHandleOperator:
    @pytest.mark.asyncio
    async def test_runs_operator_turn_under_operator_bucket(self):
        bot = _make_bot()
        bot._send_with_retry = AsyncMock()
        agent = MagicMock()
        agent.get_tools.return_value = []
        agent.chat = AsyncMock(return_value=_operator_result("ack"))
        agent.record_assistant_message = AsyncMock()
        bot._agent = agent
        # Enable TTS so the operator turn sees the full action vocabulary
        # (including send_voice_note). With TTS offline the schema legitimately
        # drops send_voice_note — covered by the offline test below.
        bot._waha = MagicMock()
        bot._waha.kokoro_enabled = True
        bot._tts_available = True

        env = await bot.handle_operator("do something", persist=False)

        agent.chat.assert_awaited_once()
        kwargs = agent.chat.call_args.kwargs
        assert kwargs["conversation_id"] == "operator"
        assert kwargs["output_cls"] is WahaAction
        assert kwargs["tools"] is not None
        # persist=False -> set_goal not in the allowlist
        assert all(t.metadata.name != "set_goal" for t in kwargs["tools"])
        assert env.ok is True
        assert env.reply == "ack"

    @pytest.mark.asyncio
    async def test_persist_registers_set_goal(self):
        bot = _make_bot()
        agent = MagicMock()
        agent.get_tools.return_value = []
        agent.chat = AsyncMock(return_value=_operator_result("noted"))
        agent.record_assistant_message = AsyncMock()
        bot._agent = agent

        await bot.handle_operator("steer", persist=True)

        kwargs = agent.chat.call_args.kwargs
        assert any(t.metadata.name == "set_goal" for t in kwargs["tools"])

    @pytest.mark.asyncio
    async def test_operator_turn_injects_tts_capability_note_when_offline(self):
        bot = _make_bot()
        bot._waha = MagicMock()
        bot._waha.kokoro_enabled = True
        bot._tts_available = False
        agent = MagicMock()
        agent.get_tools.return_value = []
        agent.chat = AsyncMock(return_value=_operator_result("ack"))
        agent.record_assistant_message = AsyncMock()
        bot._agent = agent

        await bot.handle_operator("do something", persist=False)

        kwargs = agent.chat.call_args.kwargs
        extra = kwargs["extra_system_context"]
        assert "Voice notes are currently unavailable" in extra
        assert "send_voice_note" in extra
        # Capability alters the schema: voice offline -> send_voice_note is not
        # a reachable action for this operator turn.
        assert kwargs["output_cls"] is WahaNoVoiceAction

    @pytest.mark.asyncio
    async def test_operator_turn_omits_tts_capability_note_when_online(self):
        bot = _make_bot()
        bot._waha = MagicMock()
        bot._waha.kokoro_enabled = True
        bot._tts_available = True
        agent = MagicMock()
        agent.get_tools.return_value = []
        agent.chat = AsyncMock(return_value=_operator_result("ack"))
        agent.record_assistant_message = AsyncMock()
        bot._agent = agent

        await bot.handle_operator("do something", persist=False)

        extra = agent.chat.call_args.kwargs["extra_system_context"]
        assert "Voice notes are currently unavailable" not in extra
        assert extra == bot._OPERATOR_TURN_CONTEXT

    @pytest.mark.asyncio
    async def test_dispatch_send_to_group_sends_and_records(self):
        bot = _make_bot()
        bot._send_with_retry = AsyncMock()
        agent = MagicMock()
        agent.get_tools.return_value = []
        agent.record_assistant_message = AsyncMock()
        bot._agent = agent

        result = _operator_result("hola mundo", action="send_to_group", target="group@g.us")
        env = await bot._dispatch_operator_action(result, persist=False)

        bot._send_with_retry.assert_awaited_once_with("group@g.us", "hola mundo")
        agent.record_assistant_message.assert_awaited_once_with("group@g.us", "hola mundo")
        assert env.ok is True
        assert "sent to" in env.reply
        assert "group@g.us" in env.reply

    @pytest.mark.asyncio
    async def test_dispatch_send_to_group_failure(self):
        bot = _make_bot()
        bot._send_with_retry = AsyncMock(side_effect=RuntimeError("waha down"))
        agent = MagicMock()
        agent.get_tools.return_value = []
        agent.record_assistant_message = AsyncMock()
        bot._agent = agent

        result = _operator_result("hi", action="send_to_group", target="g@g.us")
        env = await bot._dispatch_operator_action(result, persist=False)

        assert env.ok is False
        assert "failed to send" in env.reply
        agent.record_assistant_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dispatch_console_returns_reply(self):
        bot = _make_bot()
        bot._send_with_retry = AsyncMock()
        agent = MagicMock()
        agent.get_tools.return_value = []
        bot._agent = agent

        result = _operator_result("noted, will do", action="console")
        env = await bot._dispatch_operator_action(result, persist=False)

        bot._send_with_retry.assert_not_awaited()
        assert env.ok is True
        assert env.reply == "noted, will do"

    @pytest.mark.asyncio
    async def test_set_goal_tool_writes_goal_manager(self):
        bot = _make_bot()
        gm = MagicMock()
        agent = MagicMock()
        agent.goal_manager = gm
        bot._agent = agent

        tool = bot._build_set_goal_tool()
        out = await tool.acall(goal="be concise")
        assert "goal set" in str(out.content)
        gm.set_goal.assert_called_once_with("be concise")

    @pytest.mark.asyncio
    async def test_handler_exception_returns_failure_envelope(self):
        bot = _make_bot()
        agent = MagicMock()
        agent.get_tools.return_value = []
        agent.chat = AsyncMock(side_effect=RuntimeError("llm down"))
        bot._agent = agent

        env = await bot.handle_operator("x", persist=False)
        assert env.ok is False
        assert env.reply == "operator turn failed"


class TestRunRegistry:
    def test_register_get_remove_roundtrip(self, tmp_path):
        reg = RunRegistry(runs_path(tmp_path, "waha"))
        rid = generate_run_id()
        rec = RunRecord(
            endpoint="http://127.0.0.1:8000",
            hmac_key="k",
            pid=1,
            started_at="2026-01-01T00:00:00+00:00",
        )
        reg.replace(rid, rec)
        record = reg.get(rid)
        assert record is not None
        assert record.endpoint == "http://127.0.0.1:8000"
        reg.remove(rid)
        assert reg.get(rid) is None

    def test_active_prunes_dead_pid(self, tmp_path):
        reg = RunRegistry(runs_path(tmp_path, "waha"))
        rec = RunRecord(
            endpoint="http://127.0.0.1:8000",
            hmac_key="k",
            pid=999999,  # almost certainly not running
            started_at="2026-01-01T00:00:00+00:00",
        )
        reg.replace("dead1", rec)
        active = reg.active()
        assert "dead1" not in active
        # pruned from disk too
        assert reg.get("dead1") is None

    def test_active_keeps_live_pid(self, tmp_path):
        reg = RunRegistry(runs_path(tmp_path, "waha"))
        rec = RunRecord(
            endpoint="http://127.0.0.1:8000",
            hmac_key="k",
            pid=1,  # init, always alive on linux
            started_at="2026-01-01T00:00:00+00:00",
        )
        reg.replace("live1", rec)
        assert "live1" in reg.active()

    def test_pid_alive_invalid(self):
        assert pid_alive(0) is False
        assert pid_alive(-1) is False

    def test_runs_path(self, tmp_path):
        assert runs_path(tmp_path, "waha") == tmp_path / "waha.runs.json"
        assert runs_path(None, "waha") == Path("data/waha.runs.json")


class TestTellHmacSigning:
    """The CLI signs the body with the run's captured key/algorithm; the
    /tell route verifies with the same scheme as the inbound webhook."""

    @pytest.mark.asyncio
    async def test_signed_tell_body_verifies(self):
        async def on_tell(message: str, *, persist: bool = False) -> TellResult:
            return TellResult(ok=True, reply=message)

        app = create_webhook_app(hmac_key=_KEY, on_tell=on_tell)
        body = json.dumps({"message": "hi", "persist": False}).encode()
        sig = hmac_mod.new(_KEY.encode(), body, hashlib.sha512).hexdigest()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/tell",
                content=body,
                headers={"Content-Type": "application/json", "X-Webhook-Hmac": sig},
            )
            assert resp.status_code == 200
            assert resp.json()["reply"] == "hi"
