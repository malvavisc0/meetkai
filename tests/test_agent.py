import base64
import json
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
from httpx import Response
from llama_index.core.base.llms.types import TextBlock, VideoBlock
from llama_index.core.llms import ChatMessage, ChatResponse, MessageRole
from llama_index.llms.openai import utils as _openai_utils
from llama_index.llms.openai_like import OpenAILike

from kai.agent.context import MessageContext
from kai.agent.core import _DEFAULT_SYSTEM_PROMPT, ActionResult, KaiAgent
from kai.agent.goal import GoalManager
from kai.config.settings import Settings

_TEST_ACTIONS = Literal["reply", "silent", "sleep"]


class _TestAction(ActionResult):
    """A minimal action vocabulary for unit tests (reply | silent | sleep)."""

    action: _TEST_ACTIONS  # type: ignore[assignment]
    text: str | None = None


# Mirrors waha's WahaNoVoiceAction: a vocabulary that deliberately excludes
# ``send_voice_note`` (the TTS-offline case). Used to exercise the parser's
# base-class recovery when the model emits a disallowed-but-deliverable action.
_NO_VOICE_ACTIONS = Literal["reply", "silent", "sleep", "send_dm", "send_to_group"]


class _TestNoVoiceAction(ActionResult):
    """Action vocabulary excluding send_voice_note (TTS-offline analogue)."""

    action: _NO_VOICE_ACTIONS  # type: ignore[assignment]
    text: str | None = None
    target: str | None = None


def _mock_llm(reply_content: str | None = "ok", *, action: _TEST_ACTIONS = "reply"):
    """Create a mock LLM that returns a simple text reply (no tool calls).

    ``reply_content`` becomes both the raw assistant content (for
    ``achat_with_tools``) and ``action.text`` on the structured result. Pass
    ``action="silent"`` with ``reply_content=None`` to simulate a silent turn.

    The structured terminal step is wired through ``llm.achat`` so the mock
    exercises the same call path as production: ``_run_with_tools`` calls
    ``self._llm.achat(messages=...)`` once for the terminal step and parses the
    response's ``message.content`` as JSON into ``output_cls``. The mock
    serializes the action as JSON in the content rather than using a bare
    ``AsyncMock`` that would accept any signature and mask mismatches.
    """
    text = reply_content if reply_content else None
    llm = MagicMock()
    llm.achat_with_tools = AsyncMock(
        return_value=ChatResponse(
            message=ChatMessage(role=MessageRole.ASSISTANT, content=reply_content)
        )
    )
    llm.get_tool_calls_from_response = MagicMock(return_value=[])
    _wire_structured_action(llm, _TestAction(action=action, text=text))
    return llm


def _structured_action(final_text: str, *, action: _TEST_ACTIONS = "reply") -> _TestAction:
    """Build a ``_TestAction`` whose ``text`` is ``final_text`` (None if empty)."""
    text = final_text if final_text else None
    return _TestAction(action=action, text=text)


def _structured_response(action_instance: _TestAction) -> ChatResponse:
    """A ChatResponse whose content is the action serialized as JSON.

    The terminal step parses ``message.content`` (after stripping reasoning
    channels) into ``output_cls`` via ``PydanticOutputParser``, so the mock
    must hand back valid JSON rather than a pre-parsed ``raw`` object.
    """
    return ChatResponse(
        message=ChatMessage(role=MessageRole.ASSISTANT, content=action_instance.model_dump_json()),
    )


def _wire_structured(llm, final_text: str, *, action: _TEST_ACTIONS = "reply") -> None:
    """Wire ``llm.achat`` to resolve ``action``/``final_text``."""
    _wire_structured_action(llm, _structured_action(final_text, action=action))


def _wire_structured_action(llm, action_instance: _TestAction) -> None:
    """Wire ``llm.achat`` to return a fixed parsed action."""
    llm.achat = AsyncMock(return_value=_structured_response(action_instance))


def _wire_structured_side_effect(
    llm, final_texts: list[str], *, action: _TEST_ACTIONS = "reply"
) -> None:
    """Wire ``llm.achat`` with a per-call side effect (one action per text)."""
    responses = [_structured_response(_structured_action(t, action=action)) for t in final_texts]
    llm.achat = AsyncMock(side_effect=responses)


class TestKaiAgent:
    def test_default_system_prompt(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        prompt = agent._get_system_prompt()
        assert "helpful assistant" in prompt

    def test_custom_system_prompt(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.set_system_prompt("You are a joke bot.")
        prompt = agent._get_system_prompt()
        assert "joke bot" in prompt

    def test_goal_appended_to_prompt(self):
        gm = GoalManager()
        gm.set_goal("Tell jokes")
        agent = KaiAgent(settings=None, goal_manager=gm)
        prompt = agent._get_system_prompt()
        assert "Tell jokes" in prompt
        assert "Current goal" in prompt

    def test_custom_prompt_plus_goal(self):
        gm = GoalManager()
        gm.set_goal("Be funny")
        agent = KaiAgent(settings=None, goal_manager=gm)
        agent.set_system_prompt("You are a bot.")
        prompt = agent._get_system_prompt()
        assert "You are a bot." in prompt
        assert "Be funny" in prompt

    def test_system_prompt_overrides_per_call(self):
        gm = GoalManager()
        gm.set_goal("Be helpful")
        agent = KaiAgent(settings=None, goal_manager=gm)
        agent.set_system_prompt("Base prompt.")
        prompt = agent._get_system_prompt(overrides="Custom per-call prompt.")
        assert "Custom per-call prompt." in prompt
        assert "Be helpful" in prompt
        assert "Base prompt." not in prompt

    def test_extra_context_augments_base_prompt(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.set_system_prompt("Base prompt.")
        prompt = agent._get_system_prompt(extra_context="People in this chat: Alice, Bob")
        # extra_context must AUGMENT the base persona, not replace it.
        assert "Base prompt." in prompt
        assert "People in this chat: Alice, Bob" in prompt

    def test_build_messages_extra_context_keeps_base_prompt(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.set_system_prompt("Base prompt.")
        messages = agent._build_messages("Hello", extra_system_context="People in this chat: Alice")
        system = messages[0].content
        assert system is not None
        assert "Base prompt." in system
        assert "People in this chat: Alice" in system

    def test_system_prompt_includes_current_time(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        prompt = agent._get_system_prompt()
        assert "Current date and time:" in prompt
        assert "UTC:" in prompt

    def test_timezone_changes_local_time_in_prompt(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.set_timezone("Europe/Berlin")
        prompt = agent._get_system_prompt()
        assert "CEST" in prompt
        assert "Current date and time:" in prompt

    def test_set_timezone_none_falls_back_to_local(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.set_timezone(None)
        prompt = agent._get_system_prompt()
        assert "Current date and time:" in prompt

    def test_set_timezone_strips_whitespace(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.set_timezone("  Europe/Berlin  ")
        assert agent._timezone == "Europe/Berlin"

    def test_unknown_timezone_falls_back_gracefully(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.set_timezone("Not/A_Real_Zone")
        # Must not raise; falls back to local time and still produces a prompt.
        prompt = agent._get_system_prompt()
        assert "Current date and time:" in prompt

    def test_history_cleared_on_prompt_change(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent._history["chat"] = [ChatMessage(role=MessageRole.USER, content="fake")]
        agent.set_system_prompt("New prompt")
        assert len(agent._history) == 1

    def test_history_cleared_with_explicit_clear(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent._history["chat"] = [ChatMessage(role=MessageRole.USER, content="fake")]
        agent.set_system_prompt("New prompt", clear_history=True)
        assert len(agent._history) == 0

    def test_history_cleared_on_goal_change(self):
        gm = GoalManager()
        agent = KaiAgent(settings=None, goal_manager=gm)
        agent._history["chat"] = [ChatMessage(role=MessageRole.USER, content="fake")]
        gm.set_goal("New goal")

        messages = agent._build_messages("Hello", conversation_id="chat")

        system = messages[0].content
        assert system is not None
        assert "Current goal: New goal" in system
        assert messages[1].content == "fake"
        assert len(agent._history["chat"]) == 1

    def test_build_messages_structure(self):
        gm = GoalManager()
        agent = KaiAgent(settings=None, goal_manager=gm)
        agent.set_system_prompt("System instructions.")
        messages = agent._build_messages("Hello")
        assert len(messages) == 2
        assert messages[0].role.value == "system"
        system = messages[0].content
        assert system is not None
        assert "System instructions." in system
        assert messages[1].role.value == "user"
        assert messages[1].content == "Hello"

    def test_build_messages_includes_history(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent._history["default"] = [
            ChatMessage(role=MessageRole.USER, content="prev user"),
            ChatMessage(role=MessageRole.ASSISTANT, content="prev assistant"),
        ]
        messages = agent._build_messages("new message")
        assert len(messages) == 4
        assert messages[1].content == "prev user"
        assert messages[2].content == "prev assistant"
        assert messages[3].content == "new message"

    def test_build_messages_per_call_system_prompt(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.set_system_prompt("Base prompt.")
        messages = agent._build_messages("Hello", system_prompt="Override prompt.")
        system = messages[0].content
        assert system is not None
        assert "Override prompt." in system
        assert "Base prompt." not in system

    def test_trim_history(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent._max_history = 4
        for i in range(10):
            agent._get_history().append(ChatMessage(role=MessageRole.USER, content=f"msg{i}"))
        agent._trim_history()
        assert len(agent._get_history()) == 4
        assert agent._get_history()[0].content == "msg6"

    def test_trim_history_by_character_budget(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent._max_history = 10
        agent._max_history_chars = 8
        for content in ("1234", "5678", "9012"):
            agent._get_history().append(ChatMessage(role=MessageRole.USER, content=content))

        agent._trim_history()

        assert [message.content for message in agent._get_history()] == ["5678", "9012"]

    def test_trim_history_preserves_alternating_pattern(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent._max_history = 4
        agent._max_history_chars = 9999
        for i in range(6):
            agent._get_history().append(ChatMessage(role=MessageRole.USER, content=f"u{i}"))
            agent._get_history().append(ChatMessage(role=MessageRole.ASSISTANT, content=f"a{i}"))
        agent._trim_history()
        history = agent._get_history()
        assert len(history) == 4
        assert history[0].content == "u4"
        assert history[1].content == "a4"
        assert history[2].content == "u5"
        assert history[3].content == "a5"


class TestKaiAgentChat:
    @pytest.fixture
    def settings(self):
        return Settings.for_test(
            llm_api_base="http://localhost:8080/v1",
            llm_api_key="test-key",
            llm_model="test-model",
            agent_history_folder=None,
        )

    @pytest.mark.asyncio
    async def test_chat_returns_reply(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent._llm = _mock_llm("Why did the chicken?")

        result = await agent.chat("Tell me a joke", output_cls=_TestAction)
        assert result.action.text == "Why did the chicken?"
        assert result.reply == "Why did the chicken?"

    @pytest.mark.asyncio
    async def test_chat_stores_history(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent._llm = _mock_llm("Sure!")

        await agent.chat("Hello", output_cls=_TestAction)
        history = agent._get_history()
        assert len(history) == 2
        assert history[0].role == MessageRole.USER
        assert history[0].content == "Hello"
        assert history[1].role == MessageRole.ASSISTANT
        assert history[1].content == "Sure!"

    @pytest.mark.asyncio
    async def test_chat_uses_system_prompt_and_history(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent.set_system_prompt("You are a joke bot.")
        agent._llm = _mock_llm("Ha!")

        await agent.chat("first", output_cls=_TestAction)
        await agent.chat("second", output_cls=_TestAction)

        second_call = agent._llm.achat_with_tools.call_args_list[1]
        messages = second_call.kwargs["chat_history"]
        assert messages[0].role == MessageRole.SYSTEM
        assert "joke bot" in messages[0].content
        assert messages[1].content == "first"
        assert messages[2].content == "Ha!"
        assert messages[3].content == "second"

    @pytest.mark.asyncio
    async def test_chat_per_call_system_prompt(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent.set_system_prompt("Base prompt.")
        agent._llm = _mock_llm("ok")

        await agent.chat("hello", output_cls=_TestAction, system_prompt="Group: Alice, Bob")

        call_args = agent._llm.achat_with_tools.call_args_list[0]
        messages = call_args.kwargs["chat_history"]
        assert "Group: Alice, Bob" in messages[0].content
        assert "Base prompt." not in messages[0].content

    @pytest.mark.asyncio
    async def test_chat_returns_error_on_failure(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        llm = _mock_llm()
        llm.achat_with_tools.side_effect = RuntimeError("API down")
        agent._llm = llm

        result = await agent.chat("Hello", output_cls=_TestAction)
        assert result.error is not None
        assert result.action.action == "error"

    @pytest.mark.asyncio
    async def test_chat_trims_history(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent._max_history = 4
        agent._llm = _mock_llm("ok")

        for i in range(10):
            await agent.chat(f"msg{i}", output_cls=_TestAction)

        assert len(agent._get_history()) == 4

    @pytest.mark.asyncio
    async def test_chat_isolates_history_by_conversation(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent._llm = _mock_llm("ok")

        await agent.chat("from chat one", output_cls=_TestAction, conversation_id="chat-1")
        await agent.chat("from chat two", output_cls=_TestAction, conversation_id="chat-2")

        second_call = agent._llm.achat_with_tools.call_args_list[1]
        messages = second_call.kwargs["chat_history"]
        assert _DEFAULT_SYSTEM_PROMPT in messages[0].content
        assert messages[1].content == "from chat two"

    @pytest.mark.asyncio
    async def test_observe_stores_message_without_reply(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent._llm = _mock_llm("ok")

        await agent.observe("background message", conversation_id="chat-1")
        history = agent._get_history("chat-1")
        assert len(history) == 1
        assert history[0].role == MessageRole.USER
        assert history[0].content == "background message"

    @pytest.mark.asyncio
    async def test_chat_store_user_message_can_be_disabled(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent._llm = _mock_llm("ok")

        await agent.observe("user message", conversation_id="chat-1")
        await agent.chat(
            "user message",
            output_cls=_TestAction,
            conversation_id="chat-1",
            store_user_message=False,
        )
        history = agent._get_history("chat-1")
        assert len(history) == 2
        assert [m.role for m in history] == [MessageRole.USER, MessageRole.ASSISTANT]

    @pytest.mark.asyncio
    async def test_chat_guards_empty_reply(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent._llm = _mock_llm(reply_content=None)

        result = await agent.chat("Hello", output_cls=_TestAction)
        assert result.reply == ""
        assert result.action.text is None
        # An empty reply stores nothing (mirrors the old silent early-return).
        assert len(agent._get_history()) == 0

    @pytest.mark.asyncio
    async def test_chat_delegated_action_stores_user_message_only(self, settings):
        # Regression: an operator turn whose action is delegated (e.g.
        # send_to_group / send_voice_note) must still persist the operator's
        # inbound instruction in the operator history bucket — the assistant
        # reply text is recorded in the *target* chat by the bot's dispatch,
        # not here. Previously the entire history block was skipped when the
        # action was delegated, losing the operator's message.
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent._llm = _mock_llm("hola mundo")

        result = await agent.chat(
            "send a message to the group saying hello",
            output_cls=_TestAction,
            conversation_id="operator",
            is_delegated_action=lambda a: a.action == "reply",
        )

        assert result.action.text == "hola mundo"
        history = agent._get_history("operator")
        # User message IS stored (the operator's instruction).
        assert any(m.role == MessageRole.USER for m in history)
        # Assistant reply is NOT stored here (delegated to target chat).
        assert not any(m.role == MessageRole.ASSISTANT for m in history)

    def test_build_llm_disables_thinking_by_default(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        llm = agent._build_llm()
        assert (
            llm.additional_kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
        )

    def test_build_llm_enables_thinking_when_configured(self):
        settings = Settings.for_test(
            llm_api_base="http://localhost:8080/v1",
            llm_api_key="test-key",
            llm_model="test-model",
            agent_history_folder=None,
            llm_enable_thinking=True,
        )
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        llm = agent._build_llm()
        assert (
            llm.additional_kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
        )

    @pytest.mark.asyncio
    async def test_chat_persists_history(self, tmp_path, settings):
        settings.agent_history_folder = tmp_path
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent._llm = _mock_llm("stored")

        await agent.chat("remember me", output_cls=_TestAction, conversation_id="chat-1")
        await agent.flush()

        restored = KaiAgent(settings=settings, goal_manager=GoalManager())
        assert [message.content for message in restored._get_history("chat-1")] == [
            "remember me",
            "stored",
        ]

    @pytest.mark.asyncio
    async def test_chat_persists_goal_with_history(self, tmp_path, settings):
        settings.agent_history_folder = tmp_path
        gm = GoalManager()
        gm.set_goal("Support users")
        agent = KaiAgent(settings=settings, goal_manager=gm)
        agent._llm = _mock_llm("done")

        await agent.chat("hello", output_cls=_TestAction)
        await agent.flush()

        restored_gm = GoalManager()
        restored = KaiAgent(settings=settings, goal_manager=restored_gm)
        assert restored_gm.has_goal() is True
        restored_goal = restored_gm.get_goal()
        assert restored_goal is not None
        assert restored_goal.description == "Support users"
        assert restored_gm.revision == 0
        assert [message.content for message in restored._get_history("default")] == [
            "hello",
            "done",
        ]


class TestKaiAgentContext:
    @pytest.fixture
    def settings(self):
        return Settings.for_test(
            llm_api_base="http://localhost:8080/v1",
            llm_api_key="test-key",
            llm_model="test-model",
            agent_history_folder=None,
        )

    def test_format_user_message_group(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        ctx = MessageContext(
            sender_name="Juan Palotes",
            sender_id="123456789012345@lid",
            conversation_id="123@g.us",
            multi_party=True,
        )
        result = agent._format_user_message("hello", ctx)
        assert result == "[Juan Palotes] hello"

    def test_format_user_message_group_mentions_bot(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        ctx = MessageContext(
            sender_name="Juan Palotes",
            sender_id="123456789012345@lid",
            conversation_id="123@g.us",
            multi_party=True,
            addressed_to_bot=True,
        )
        result = agent._format_user_message("hello", ctx)
        assert result == "[Juan Palotes (addressing you)] hello"

    def test_format_user_message_dm(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        ctx = MessageContext(
            sender_name="Lucerna",
            sender_id="12345678901234@lid",
            conversation_id="12345678901234@lid",
            multi_party=False,
        )
        result = agent._format_user_message("hi", ctx)
        assert result == "[Lucerna] hi"

    def test_format_user_message_no_context(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        result = agent._format_user_message("hello", None)
        assert result == "hello"

    def test_get_system_prompt_no_roster_injection(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        prompt = agent._get_system_prompt()
        assert "People in this chat" not in prompt
        assert "@[Name]" not in prompt

    @pytest.mark.asyncio
    async def test_silent_reply_not_stored_in_history(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent._llm = _mock_llm(action="silent", reply_content=None)

        result = await agent.chat("hello", output_cls=_TestAction)
        assert result.action.action == "silent"
        assert result.reply == ""
        # A silent turn stores nothing in history.
        history = agent._get_history()
        assert len(history) == 0

    @pytest.mark.asyncio
    async def test_chat_formats_message_with_context(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent._llm = _mock_llm("hi back")

        ctx = MessageContext(
            sender_name="Lucerna",
            sender_id="456@lid",
            conversation_id="456@lid",
            multi_party=False,
        )
        await agent.chat("hello", output_cls=_TestAction, context=ctx)

        call_args = agent._llm.achat_with_tools.call_args_list[0]
        messages = call_args.kwargs["chat_history"]
        assert "[Lucerna] hello" in messages[1].content


class TestKaiAgentToolHistory:
    @pytest.fixture
    def settings(self):
        return Settings.for_test(
            llm_api_base="http://localhost:8080/v1",
            llm_api_key="test-key",
            llm_model="test-model",
            agent_history_folder=None,
        )

    @staticmethod
    def _make_tool_call(name="web_search", tool_id="tc_1"):
        tc = MagicMock()
        tc.tool_name = name
        tc.tool_kwargs = {"query": "test"}
        tc.tool_id = tool_id
        return tc

    @pytest.mark.asyncio
    async def test_tool_calls_not_in_history(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())

        tc = self._make_tool_call()
        tool_response = ChatResponse(message=ChatMessage(role=MessageRole.ASSISTANT, content=None))
        final_response = ChatResponse(
            message=ChatMessage(role=MessageRole.ASSISTANT, content="Here are the results")
        )

        mock_tool = AsyncMock()
        mock_tool.acall.return_value = MagicMock(content="[{'title': 'Result', 'url': 'http://x'}]")
        agent._tools_by_name["web_search"] = mock_tool

        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(side_effect=[tool_response, final_response])
        mock_llm.get_tool_calls_from_response = MagicMock(side_effect=[[tc], []])
        _wire_structured(mock_llm, "Here are the results")
        agent._llm = mock_llm

        result = await agent.chat("search for test", output_cls=_TestAction)
        assert result.action.text == "Here are the results"

        history = agent._get_history()
        assert len(history) == 2
        assert history[0].role == MessageRole.USER
        assert history[0].content == "search for test"
        assert history[1].role == MessageRole.ASSISTANT
        assert history[1].content == "Here are the results"

    @pytest.mark.asyncio
    async def test_tool_call_callback_fired(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())

        tc = self._make_tool_call()
        tool_response = ChatResponse(message=ChatMessage(role=MessageRole.ASSISTANT, content=None))
        final_response = ChatResponse(
            message=ChatMessage(role=MessageRole.ASSISTANT, content="done")
        )

        mock_tool = AsyncMock()
        mock_tool.acall.return_value = MagicMock(content="result text")
        agent._tools_by_name["web_search"] = mock_tool

        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(side_effect=[tool_response, final_response])
        mock_llm.get_tool_calls_from_response = MagicMock(side_effect=[[tc], []])
        _wire_structured(mock_llm, "done")
        agent._llm = mock_llm

        calls: list[tuple] = []
        agent.set_tool_call_callback(
            lambda name, kwargs, result: calls.append((name, kwargs, result))
        )

        await agent.chat("search for test", output_cls=_TestAction)

        assert len(calls) == 1
        name, kwargs, result = calls[0]
        assert name == "web_search"
        assert kwargs == {"query": "test"}
        assert result == "result text"

    @pytest.mark.asyncio
    async def test_tool_call_callback_not_fired_when_none_registered(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        assert agent._tool_call_callback is None  # no callback = no error

        tc = self._make_tool_call()
        tool_response = ChatResponse(message=ChatMessage(role=MessageRole.ASSISTANT, content=None))
        final_response = ChatResponse(
            message=ChatMessage(role=MessageRole.ASSISTANT, content="done")
        )
        mock_tool = AsyncMock()
        mock_tool.acall.return_value = MagicMock(content="x")
        agent._tools_by_name["web_search"] = mock_tool
        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(side_effect=[tool_response, final_response])
        mock_llm.get_tool_calls_from_response = MagicMock(side_effect=[[tc], []])
        _wire_structured(mock_llm, "done")
        agent._llm = mock_llm

        # No callback set; must not raise.
        await agent.chat("search for test", output_cls=_TestAction)

    @pytest.mark.asyncio
    async def test_tool_call_callback_exception_swallowed(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())

        tc = self._make_tool_call()
        tool_response = ChatResponse(message=ChatMessage(role=MessageRole.ASSISTANT, content=None))
        final_response = ChatResponse(
            message=ChatMessage(role=MessageRole.ASSISTANT, content="done")
        )
        mock_tool = AsyncMock()
        mock_tool.acall.return_value = MagicMock(content="x")
        agent._tools_by_name["web_search"] = mock_tool
        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(side_effect=[tool_response, final_response])
        mock_llm.get_tool_calls_from_response = MagicMock(side_effect=[[tc], []])
        agent._llm = mock_llm

        def bad_callback(name, kwargs, result):
            raise RuntimeError("callback boom")

        agent.set_tool_call_callback(bad_callback)
        # A faulty callback must not break the agent flow.
        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(side_effect=[tool_response, final_response])
        mock_llm.get_tool_calls_from_response = MagicMock(side_effect=[[tc], []])
        _wire_structured(mock_llm, "done")
        agent._llm = mock_llm
        result = await agent.chat("search for test", output_cls=_TestAction)
        assert result.action.text == "done"

    @pytest.mark.asyncio
    async def test_tool_error_does_not_leak_into_history(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())

        tc = self._make_tool_call(name="unknown_tool")
        tool_response = ChatResponse(message=ChatMessage(role=MessageRole.ASSISTANT, content=None))
        final_response = ChatResponse(
            message=ChatMessage(role=MessageRole.ASSISTANT, content="I tried but it failed")
        )

        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(side_effect=[tool_response, final_response])
        mock_llm.get_tool_calls_from_response = MagicMock(side_effect=[[tc], []])
        _wire_structured(mock_llm, "I tried but it failed")
        agent._llm = mock_llm

        result = await agent.chat("do something", output_cls=_TestAction)
        assert result.action.text == "I tried but it failed"

        history = agent._get_history()
        assert len(history) == 2
        for msg in history:
            assert "Error" not in (msg.content or "")

    @pytest.mark.asyncio
    async def test_multiple_tool_rounds_history_clean(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())

        tc1 = self._make_tool_call(name="web_search", tool_id="tc_1")
        tc2 = self._make_tool_call(name="get_webpage_html", tool_id="tc_2")

        round1 = ChatResponse(message=ChatMessage(role=MessageRole.ASSISTANT, content=None))
        round2 = ChatResponse(message=ChatMessage(role=MessageRole.ASSISTANT, content=None))
        final = ChatResponse(
            message=ChatMessage(role=MessageRole.ASSISTANT, content="Combined answer")
        )

        mock_tool_search = AsyncMock()
        mock_tool_search.acall.return_value = MagicMock(content="[search results]")
        mock_tool_fetch = AsyncMock()
        mock_tool_fetch.acall.return_value = MagicMock(content="Page content")
        agent._tools_by_name["web_search"] = mock_tool_search
        agent._tools_by_name["get_webpage_html"] = mock_tool_fetch

        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(side_effect=[round1, round2, final])
        mock_llm.get_tool_calls_from_response = MagicMock(side_effect=[[tc1], [tc2], []])
        _wire_structured(mock_llm, "Combined answer")
        agent._llm = mock_llm

        result = await agent.chat("find info about cats", output_cls=_TestAction)
        assert result.action.text == "Combined answer"

        history = agent._get_history()
        assert len(history) == 2
        assert history[0].role == MessageRole.USER
        assert history[1].role == MessageRole.ASSISTANT
        assert history[1].content == "Combined answer"

    @pytest.mark.asyncio
    async def test_max_tool_rounds_exceeded(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())

        tc = self._make_tool_call()
        tool_response = ChatResponse(message=ChatMessage(role=MessageRole.ASSISTANT, content=None))

        mock_tool = AsyncMock()
        mock_tool.acall.return_value = MagicMock(content="result")
        agent._tools_by_name["web_search"] = mock_tool

        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(return_value=tool_response)
        mock_llm.get_tool_calls_from_response = MagicMock(return_value=[tc])
        _wire_structured(mock_llm, "done")
        agent._llm = mock_llm

        result = await agent.chat("infinite loop", output_cls=_TestAction)
        assert result.action.text == "done"

        history = agent._get_history()
        assert len(history) == 2
        assert history[0].role == MessageRole.USER
        assert history[1].role == MessageRole.ASSISTANT

    @pytest.mark.asyncio
    async def test_tool_result_passed_to_next_llm_call(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())

        tc = self._make_tool_call(name="web_search", tool_id="tc_1")

        tool_response = ChatResponse(message=ChatMessage(role=MessageRole.ASSISTANT, content=None))
        final_response = ChatResponse(
            message=ChatMessage(role=MessageRole.ASSISTANT, content="It's sunny")
        )

        mock_tool = AsyncMock()
        mock_tool.acall.return_value = MagicMock(content="Weather: 25°C, sunny")
        agent._tools_by_name["web_search"] = mock_tool

        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(side_effect=[tool_response, final_response])
        mock_llm.get_tool_calls_from_response = MagicMock(side_effect=[[tc], []])
        _wire_structured(mock_llm, "It's sunny")
        agent._llm = mock_llm

        await agent.chat("what's the weather", output_cls=_TestAction)

        second_call = mock_llm.achat_with_tools.call_args_list[1]
        scratchpad = second_call.kwargs["chat_history"]
        tool_msgs = [m for m in scratchpad if m.role == MessageRole.TOOL]
        assert len(tool_msgs) == 1
        assert "Weather: 25°C, sunny" in tool_msgs[0].content
        assert tool_msgs[0].additional_kwargs.get("tool_call_id") == "tc_1"

    @pytest.mark.asyncio
    async def test_unknown_tool_error_reported(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())

        tc = self._make_tool_call(name="nonexistent_tool")
        tool_response = ChatResponse(message=ChatMessage(role=MessageRole.ASSISTANT, content=None))
        final_response = ChatResponse(
            message=ChatMessage(role=MessageRole.ASSISTANT, content="Done")
        )

        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(side_effect=[tool_response, final_response])
        mock_llm.get_tool_calls_from_response = MagicMock(side_effect=[[tc], []])
        _wire_structured(mock_llm, "Done")
        agent._llm = mock_llm

        result = await agent.chat("use the tool", output_cls=_TestAction)
        assert result.action.text == "Done"

        second_call = mock_llm.achat_with_tools.call_args_list[1]
        scratchpad = second_call.kwargs["chat_history"]
        tool_msgs = [m for m in scratchpad if m.role == MessageRole.TOOL]
        assert len(tool_msgs) == 1
        assert "unknown tool" in tool_msgs[0].content

    @pytest.mark.asyncio
    async def test_tool_exception_reported(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())

        tc = self._make_tool_call(name="web_search")
        tool_response = ChatResponse(message=ChatMessage(role=MessageRole.ASSISTANT, content=None))
        final_response = ChatResponse(
            message=ChatMessage(role=MessageRole.ASSISTANT, content="Had an issue")
        )

        mock_tool = AsyncMock()
        mock_tool.acall.side_effect = RuntimeError("Connection timeout")
        agent._tools_by_name["web_search"] = mock_tool

        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(side_effect=[tool_response, final_response])
        mock_llm.get_tool_calls_from_response = MagicMock(side_effect=[[tc], []])
        _wire_structured(mock_llm, "Had an issue")
        agent._llm = mock_llm

        result = await agent.chat("search something", output_cls=_TestAction)
        assert result.action.text == "Had an issue"

        second_call = mock_llm.achat_with_tools.call_args_list[1]
        scratchpad = second_call.kwargs["chat_history"]
        tool_msgs = [m for m in scratchpad if m.role == MessageRole.TOOL]
        assert len(tool_msgs) == 1
        assert "Connection timeout" in tool_msgs[0].content

    @pytest.mark.asyncio
    async def test_history_preserves_turn_ordering_across_tool_calls(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent._max_history = 10
        agent._max_history_chars = 9999

        tc = self._make_tool_call()
        tool_response = ChatResponse(message=ChatMessage(role=MessageRole.ASSISTANT, content=None))
        final1 = ChatResponse(message=ChatMessage(role=MessageRole.ASSISTANT, content="answer one"))
        plain2 = ChatResponse(message=ChatMessage(role=MessageRole.ASSISTANT, content="answer two"))

        mock_tool = AsyncMock()
        mock_tool.acall.return_value = MagicMock(content="tool result")
        agent._tools_by_name["web_search"] = mock_tool

        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(side_effect=[tool_response, final1, plain2])
        mock_llm.get_tool_calls_from_response = MagicMock(side_effect=[[tc], [], []])
        _wire_structured_side_effect(mock_llm, ["answer one", "answer two"])
        agent._llm = mock_llm

        await agent.chat("question one", output_cls=_TestAction)
        await agent.chat("question two", output_cls=_TestAction)

        history = agent._get_history()
        assert len(history) == 4
        assert history[0].role == MessageRole.USER
        assert history[0].content == "question one"
        assert history[1].role == MessageRole.ASSISTANT
        assert history[1].content == "answer one"
        assert history[2].role == MessageRole.USER
        assert history[2].content == "question two"
        assert history[3].role == MessageRole.ASSISTANT
        assert history[3].content == "answer two"


class TestAuditFixes:
    @pytest.fixture
    def settings(self):
        return Settings.for_test(
            llm_api_base="http://localhost:8080/v1",
            llm_api_key="test-key",
            llm_model="test-model",
            agent_history_folder=None,
        )

    def test_strip_reasoning_channels_removes_channel_blocks(self):
        from kai.agent.core import strip_reasoning_channels

        leaked = "<|channel>thought\n<channel|><|channel>thought\n<channel|>visible reply"
        assert strip_reasoning_channels(leaked) == "visible reply"

    def test_strip_reasoning_channels_keeps_visible_content(self):
        from kai.agent.core import strip_reasoning_channels

        text = "<|channel>thought\nlet me consider<channel|>Here is my reply"
        assert strip_reasoning_channels(text) == "Here is my reply"

    def test_strip_reasoning_channels_handles_stray_tokens(self):
        from kai.agent.core import strip_reasoning_channels

        # A block fully wrapping content with nothing outside → empty (the
        # model produced only reasoning, no visible reply).
        assert strip_reasoning_channels("<|channel>hi there<channel|>") == ""

    @pytest.mark.asyncio
    async def test_action_text_strips_reasoning_channels(self, settings):
        # The terminal structured step strips reasoning-model channel artifacts
        # from ``action.text`` before handing it back.
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        llm = MagicMock()
        llm.achat_with_tools = AsyncMock(
            return_value=ChatResponse(message=ChatMessage(role=MessageRole.ASSISTANT, content=None))
        )
        llm.get_tool_calls_from_response = MagicMock(return_value=[])
        _wire_structured_action(
            llm,
            _TestAction(
                action="reply",
                text="<|channel>thought<channel|>Here is my reply",
            ),
        )
        agent._llm = llm

        result = await agent.chat("hello", output_cls=_TestAction)
        assert result.action.text == "Here is my reply"

    def test_history_persists_across_prompt_change(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent._history["chat"] = [ChatMessage(role=MessageRole.USER, content="saved")]
        agent.set_system_prompt("Different prompt")
        assert len(agent._history["chat"]) == 1
        assert agent._history["chat"][0].content == "saved"

    def test_goal_change_preserves_history(self):
        gm = GoalManager()
        agent = KaiAgent(settings=None, goal_manager=gm)
        agent._history["chat"] = [ChatMessage(role=MessageRole.USER, content="kept")]
        gm.set_goal("New goal")
        agent._build_messages("Hello", conversation_id="chat")
        assert agent._history["chat"][0].content == "kept"

    @pytest.mark.asyncio
    async def test_goal_persisted_separately(self, tmp_path, settings):
        settings.agent_history_folder = tmp_path
        history_file = tmp_path / "default.json"
        gm = GoalManager()
        gm.set_goal("Test goal")
        agent = KaiAgent(settings=settings, goal_manager=gm)
        agent._llm = _mock_llm("ok")

        await agent.chat("hello", output_cls=_TestAction)
        await agent.flush()

        goal_file = tmp_path / "default.json.goal"
        assert goal_file.exists()

        import json

        data = json.loads(goal_file.read_text())
        assert data["goal"] == "Test goal"

        history_data = json.loads(history_file.read_text())
        assert "goal" not in history_data

    @pytest.mark.asyncio
    async def test_load_goal_does_not_bump_revision(self, tmp_path, settings):
        settings.agent_history_folder = tmp_path
        goal_file = tmp_path / "default.json.goal"
        goal_file.write_text('{"goal": "Restored goal"}')

        gm = GoalManager()
        KaiAgent(settings=settings, goal_manager=gm)
        assert gm.has_goal() is True
        restored_goal = gm.get_goal()
        assert restored_goal is not None
        assert restored_goal.description == "Restored goal"
        assert gm.revision == 0

    def test_tool_registration(self):
        from llama_index.core.tools import FunctionTool

        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        initial_count = len(agent._tools)

        def custom_tool(x: str) -> str:
            return x

        tool = FunctionTool.from_defaults(fn=custom_tool, name="custom_test")
        agent.register_tool(tool)
        assert len(agent._tools) == initial_count + 1
        assert "custom_test" in agent._tools_by_name

        agent.unregister_tool("custom_test")
        assert len(agent._tools) == initial_count
        assert "custom_test" not in agent._tools_by_name

    def test_clear_tools_drops_defaults(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        assert len(agent._tools) > 0
        agent.clear_tools()
        assert agent._tools == []
        assert agent._tools_by_name == {}
        assert agent._tool_instructions == ""

    def test_set_tool_workflow_appends_to_instructions(self):
        from kai.agent.tools import WEB_WORKFLOW_INSTRUCTIONS

        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        # Default: clean slate, no chat-bot workflow baked in.
        assert "fact-checking" not in agent._tool_instructions.lower()
        agent.set_tool_workflow(WEB_WORKFLOW_INSTRUCTIONS)
        assert "fact-checking" in agent._tool_instructions
        prompt = agent._get_system_prompt()
        assert "fact-checking" in prompt
        # Turning it back off removes the workflow from the rendered prompt.
        agent.set_tool_workflow(None)
        assert "fact-checking" not in agent._get_system_prompt()

    def test_clear_tools_then_register_keeps_clean_slate(self):
        from llama_index.core.tools import FunctionTool

        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.clear_tools()

        def docker_inspect(container: str) -> str:
            return "ok"

        tool = FunctionTool.from_defaults(fn=docker_inspect, name="docker_inspect")
        agent.register_tool(tool)
        assert len(agent._tools) == 1
        # Only the registered tool appears in instructions; no web-search table rows.
        assert "docker_inspect" in agent._tool_instructions
        assert "web_search" not in agent._tool_instructions

    def test_reply_style_appended(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.set_system_prompt("Base.")
        prompt = agent._get_system_prompt(reply_style="Be very brief.")
        assert "Be very brief." in prompt

    def test_allow_silence_is_a_schema_decision(self):
        # ``allow_silence`` is no longer a runtime prompt flag; "never go
        # silent" is expressed by omitting ``silent`` from the bot's action
        # ``Literal``. The prompt no longer carries a ``<<silent>>`` directive.
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.set_system_prompt("Base.")
        prompt = agent._get_system_prompt()
        assert "<<silent>>" not in prompt
        assert "Never output" not in prompt

    @pytest.mark.asyncio
    async def test_chat_with_reply_style(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent._llm = _mock_llm("ok")
        await agent.chat("hello", output_cls=_TestAction, reply_style="Max 1 sentence.")
        call_args = agent._llm.achat_with_tools.call_args_list[0]
        messages = call_args.kwargs["chat_history"]
        assert "Max 1 sentence." in messages[0].content

    @pytest.mark.asyncio
    async def test_observe_skips_empty(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent._llm = _mock_llm("ok")
        await agent.observe("", conversation_id="test")
        history = agent._get_history("test")
        assert len(history) == 0


class TestBotHistoryFolderIsolation:
    def test_history_file_resolved_from_folder_and_namespace(self, tmp_path):
        settings = Settings.for_test(
            llm_api_base="http://localhost:8080/v1",
            llm_api_key="test-key",
            agent_history_folder=tmp_path,
        )
        agent = KaiAgent(settings=settings, goal_manager=GoalManager(), namespace="waha")
        assert agent._history_file == tmp_path / "waha.json"

    def test_history_file_default_namespace(self, tmp_path):
        settings = Settings.for_test(
            llm_api_base="http://localhost:8080/v1",
            llm_api_key="test-key",
            agent_history_folder=tmp_path,
        )
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        assert agent._history_file == tmp_path / "default.json"

    def test_history_file_none_when_folder_unset(self):
        settings = Settings.for_test(
            llm_api_base="http://localhost:8080/v1",
            llm_api_key="test-key",
            agent_history_folder=None,
        )
        agent = KaiAgent(settings=settings, goal_manager=GoalManager(), namespace="waha")
        assert agent._history_file is None

    def test_per_bot_history_isolation(self, tmp_path):
        folder = tmp_path / "data"
        settings_waha = Settings.for_test(
            llm_api_base="http://localhost:8080/v1",
            llm_api_key="test-key",
            agent_history_folder=folder,
        )
        settings_email = Settings.for_test(
            llm_api_base="http://localhost:8080/v1",
            llm_api_key="test-key",
            agent_history_folder=folder,
        )

        agent_waha = KaiAgent(settings=settings_waha, goal_manager=GoalManager(), namespace="waha")
        agent_email = KaiAgent(
            settings=settings_email, goal_manager=GoalManager(), namespace="email"
        )

        agent_waha._history["chat"] = [ChatMessage(role=MessageRole.USER, content="waha msg")]
        agent_email._history["chat"] = [ChatMessage(role=MessageRole.USER, content="email msg")]

        assert agent_waha._history_file == folder / "waha.json"
        assert agent_email._history_file == folder / "email.json"
        assert agent_waha._history["chat"][0].content == "waha msg"
        assert agent_email._history["chat"][0].content == "email msg"


class TestHistoryEdits:
    @pytest.fixture
    def settings(self):
        return Settings.for_test(
            llm_api_base="http://localhost:8080/v1",
            llm_api_key="test-key",
            llm_model="test-model",
            agent_history_folder=None,
        )

    def test_history_key_namespaced_by_bot(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager(), namespace="waha")
        assert agent._history_key("123@c.us") == "waha:123@c.us"
        assert agent._history_key(None) == "waha:default"

    def test_history_key_unnamespaced_without_namespace(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        assert agent._history_key("123@c.us") == "123@c.us"
        assert agent._history_key(None) == "default"

    def test_namespaced_agents_keep_separate_history(self):
        a = KaiAgent(settings=None, goal_manager=GoalManager(), namespace="waha")
        b = KaiAgent(settings=None, goal_manager=GoalManager(), namespace="email")
        a._get_history("shared-id").append(ChatMessage(role=MessageRole.USER, content="waha"))
        b._get_history("shared-id").append(ChatMessage(role=MessageRole.USER, content="email"))
        assert a._get_history("shared-id")[0].content == "waha"
        assert b._get_history("shared-id")[0].content == "email"

    def test_history_conversations_evicted_beyond_cap(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent._max_conversations = 3
        for i in range(5):
            agent._get_history(f"chat-{i}").append(
                ChatMessage(role=MessageRole.USER, content=f"msg-{i}")
            )
        assert len(agent._history) == 3
        # oldest conversations evicted (LRU)
        assert "chat-0" not in agent._history
        assert "chat-1" not in agent._history
        assert "chat-4" in agent._history

    @pytest.mark.asyncio
    async def test_debounced_save_flushed_on_flush(self, tmp_path, settings):
        settings.agent_history_folder = tmp_path
        history_file = tmp_path / "default.json"
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent._llm = _mock_llm("stored")

        await agent.chat("remember me", output_cls=_TestAction, conversation_id="chat-1")
        # Before flush, the debounced write may not have landed yet.
        await agent.flush()
        assert history_file.exists()
        data = json.loads(history_file.read_text())
        assert any("remember me" in m["content"] for m in data["history"]["chat-1"])

    @pytest.mark.asyncio
    async def test_observe_empty_with_images_stored(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent._llm = _mock_llm("ok")
        await agent.observe("", conversation_id="test", images=[b"\x89PNG"])
        history = agent._get_history("test")
        assert len(history) == 1
        content = history[0].content
        assert content is not None
        assert "image(s)" in content


class TestStructuredPredictionContract:
    """Regression guard for the structured terminal step.

    Earlier the agent called ``llm.astructured_predict(output_cls,
    chat_history=...)``, but that method requires a ``PromptTemplate`` —
    ``chat_history`` is not a valid kwarg. The unit tests never caught it
    because they replaced the method with a bare ``AsyncMock`` that accepts
    any signature.

    These tests use a *real* ``OpenAILike`` (real bound methods with real
    signatures) behind a respx-mocked HTTP transport, so a wrong-signature
    call surfaces as a real ``TypeError`` instead of being swallowed.
    """

    @pytest.fixture
    def settings(self):
        return Settings.for_test(
            llm_api_base="http://localhost:8080/v1",
            llm_api_key="test-key",
            llm_model="test-model",
            agent_history_folder=None,
        )

    @staticmethod
    def _real_llm() -> OpenAILike:
        return OpenAILike(
            model="test-model",
            api_key="test-key",
            api_base="http://localhost:8080/v1",
            is_chat_model=True,
        )

    @staticmethod
    def _chat_completion(content: str | None) -> dict:
        return {
            "id": "1",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "model": "test-model",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_terminal_structured_step_uses_real_api(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent._llm = self._real_llm()

        # 1st request: the tool-calling loop (no tool calls -> loop breaks).
        # 2nd request: the terminal structured step; the default pydantic
        # program extracts the action JSON from the assistant content.
        respx.post(url__regex=r".*").mock(
            side_effect=[
                Response(200, json=self._chat_completion(None)),
                Response(
                    200,
                    json=self._chat_completion(json.dumps({"action": "reply", "text": "hi"})),
                ),
            ]
        )

        result = await agent.chat("hello", output_cls=_TestAction, tools=[])

        assert result.error is None
        assert result.action.action == "reply"
        assert result.action.text == "hi"

    @respx.mock
    @pytest.mark.asyncio
    async def test_disallowed_action_recovers_via_fallback(self, settings):
        # Regression for the waha TTS-offline crash: the operator asked for a
        # voice note, but ``send_voice_note`` is not in this turn's schema
        # (TTS offline). The model obeyed the user and emitted it anyway. The
        # parser must not crash with a generic "Agent chat error" — it should
        # recover via the base ActionResult so the bot's dispatch can degrade
        # (waha's send_voice_note path falls back to text delivery).
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent._llm = self._real_llm()

        fenced = (
            "```json\n"
            '{"action": "send_voice_note", "text": "wow esto es muy '
            'interesante", "target": "120360000000000000@g.us"}\n```'
        )
        respx.post(url__regex=r".*").mock(
            side_effect=[
                Response(200, json=self._chat_completion(None)),
                Response(200, json=self._chat_completion(fenced)),
                # Retry after the correction prompt also emits send_voice_note
                # (clean JSON this time) — the user insisted, so the model
                # keeps the disallowed action. Recovery must still kick in.
                Response(
                    200,
                    json=self._chat_completion(
                        json.dumps(
                            {
                                "action": "send_voice_note",
                                "text": "wow esto es muy interesante",
                                "target": "120360000000000000@g.us",
                            }
                        )
                    ),
                ),
            ]
        )

        result = await agent.chat(
            "send a voice note to 120360000000000000@g.us saying wow this is "
            "very interesting but in spanish",
            output_cls=_TestNoVoiceAction,
            tools=[],
        )

        # No crash: the turn resolves to the recovered action, preserving the
        # text and target the model produced so dispatch can deliver them.
        assert result.error is None
        assert result.action.action == "send_voice_note"
        assert result.action.text == "wow esto es muy interesante"
        assert result.action.target == "120360000000000000@g.us"

    def test_parse_structured_text_recovers_disallowed_action(self):
        # Direct unit test of the parser recovery path: a clean JSON payload
        # whose ``action`` is outside the constrained vocabulary validates
        # against the base ActionResult and is returned unchanged.
        from llama_index.core.output_parsers.pydantic import PydanticOutputParser

        parser = PydanticOutputParser(output_cls=_TestNoVoiceAction)
        payload = json.dumps(
            {
                "action": "send_voice_note",
                "text": "hola",
                "target": "g@g.us",
            }
        )

        action = KaiAgent._parse_structured_text(payload, _TestNoVoiceAction, parser)

        assert action.action == "send_voice_note"
        assert action.text == "hola"
        assert action.target == "g@g.us"

    def test_parse_structured_text_still_raises_on_malformed(self):
        # Recovery is scoped to a well-formed payload with a disallowed
        # ``action`` value. Genuinely malformed output (no action field, or
        # an action that IS allowed but other fields are broken) must still
        # raise so silent corruption never reaches dispatch.
        from llama_index.core.output_parsers.pydantic import PydanticOutputParser

        parser = PydanticOutputParser(output_cls=_TestNoVoiceAction)
        # No JSON object at all.
        with pytest.raises(ValueError):
            KaiAgent._parse_structured_text("just some prose", _TestNoVoiceAction, parser)
        # JSON object missing the action key.
        with pytest.raises(ValueError):
            KaiAgent._parse_structured_text(json.dumps({"text": "hi"}), _TestNoVoiceAction, parser)

    def test_parse_structured_text_does_not_recover_control_action(self):
        # A no-silent turn excludes ``silent`` from its vocabulary. If the
        # model emits ``silent`` anyway, recovery must NOT kick in: returning
        # silent would ghost the user on a turn designed to forbid silence
        # and bypass the bot's error-retry safety net. The parse must raise
        # so the bot's error path (retry-nudge on direct address) runs.
        from typing import Literal as _Literal

        from llama_index.core.output_parsers.pydantic import PydanticOutputParser

        class _TestNoSilentAction(ActionResult):
            action: _Literal["reply", "sleep", "send_dm", "send_to_group"]  # type: ignore[assignment]
            text: str | None = None

        parser = PydanticOutputParser(output_cls=_TestNoSilentAction)
        # silent with no text (the normal case) -> raises.
        with pytest.raises(ValueError):
            KaiAgent._parse_structured_text(
                json.dumps({"action": "silent"}), _TestNoSilentAction, parser
            )
        # silent WITH text (contradictory) -> still raises; the control-action
        # guard must dominate the "has text" heuristic.
        with pytest.raises(ValueError):
            KaiAgent._parse_structured_text(
                json.dumps({"action": "silent", "text": "ghost me"}),
                _TestNoSilentAction,
                parser,
            )


class TestVideoBlockShim:
    """The import-time monkeypatch in ``kai.agent.core`` makes llama_index's
    OpenAI adapter serialize ``VideoBlock`` as the verified ``video_url``
    Chat Completions shape. Without the shim
    the adapter raises ``ValueError("Unsupported content block type")``.
    """

    def test_video_block_emits_video_url_data_uri(self):
        mp4 = b"\x00\x00\x00\x1cftypmp42" + b"\x00" * 64
        message = ChatMessage(
            role=MessageRole.USER,
            blocks=[
                TextBlock(text="describe this"),
                VideoBlock(video=mp4, video_mimetype="video/mp4"),
            ],
        )
        out = _openai_utils.to_openai_message_dict(message)
        assert out["role"] == "user"
        content = out["content"]
        assert isinstance(content, list)
        types = [block["type"] for block in content]
        assert types == ["text", "video_url"]
        video_block = content[1]
        expected_url = f"data:video/mp4;base64,{base64.b64encode(mp4).decode()}"
        assert video_block == {"type": "video_url", "video_url": {"url": expected_url}}

    def test_text_only_message_delegates_to_original(self):
        # A message with no VideoBlock must go through the original converter
        # unchanged (string content, not a content-block list).
        message = ChatMessage(role=MessageRole.USER, content="just text")
        out = _openai_utils.to_openai_message_dict(message)
        content = out.get("content")
        assert content == "just text"

    def test_video_block_with_url_uses_url_directly(self):
        message = ChatMessage(
            role=MessageRole.USER,
            blocks=[VideoBlock(url="https://example.com/clip.mp4", video_mimetype="video/mp4")],
        )
        out = _openai_utils.to_openai_message_dict(message)
        content = out.get("content")
        assert isinstance(content, list)
        assert content[0] == {
            "type": "video_url",
            "video_url": {"url": "https://example.com/clip.mp4"},
        }

    def test_build_messages_attaches_video_block(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        mp4 = b"\x00\x00\x00\x1cftypmp42"
        messages = agent._build_messages("hi", videos=[mp4])
        user_msg = messages[-1]
        assert user_msg.role == MessageRole.USER
        block_types = [type(b).__name__ for b in user_msg.blocks]
        assert block_types == ["TextBlock", "VideoBlock"]
