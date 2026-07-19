"""Tests for the conversation tools (get_conversation_messages, record_note).

These tools operate on KaiAgent's internal _history store, not on any
transport API. The tests verify:
- get_conversation_messages reads from the agent's history
- record_note writes a [note]-prefixed assistant message
- ToolContext fallback works when conversation_id is empty
- Errors are returned as strings when no conversation can be resolved
"""

import pytest
from llama_index.core.llms import ChatMessage, MessageRole

from kai.agent.context import ToolContext
from kai.agent.core import KaiAgent
from kai.agent.goal import GoalManager
from kai.agent.tools.conversation import (
    _NOTE_PREFIX,
    _format_history,
    register_conversation_tools,
)
from kai.config.settings import Settings


def _make_agent() -> KaiAgent:
    settings = Settings.for_test(
        llm_api_base="http://localhost:8080/v1",
        llm_api_key="test-key",
        llm_model="test-model",
        agent_history_folder=None,
    )
    return KaiAgent(settings=settings, goal_manager=GoalManager())


class TestFormatHistory:
    def test_renders_user_and_assistant_messages(self):
        messages = [
            ChatMessage(role=MessageRole.USER, content="hello"),
            ChatMessage(role=MessageRole.ASSISTANT, content="hi there"),
        ]
        result = _format_history(messages)
        assert "[User] hello" in result
        assert "[Assistant] hi there" in result

    def test_skips_empty_content(self):
        messages = [
            ChatMessage(role=MessageRole.USER, content=""),
            ChatMessage(role=MessageRole.ASSISTANT, content="real reply"),
        ]
        result = _format_history(messages)
        assert "[User]" not in result
        assert "[Assistant] real reply" in result

    def test_returns_no_messages_for_empty_list(self):
        assert _format_history([]) == "No messages found."


async def _call_tool(tool, **kwargs) -> str:
    """Invoke a FunctionTool async and return its string content."""
    output = await tool.acall(**kwargs)
    return str(output.content)


class TestGetConversationMessages:
    @pytest.mark.asyncio
    async def test_reads_history_for_conversation_id(self):
        agent = _make_agent()
        await agent.observe("hello from alice", conversation_id="alice@example.com")
        await agent.record_assistant_message("alice@example.com", "hi alice")

        tools: list = []
        agent.register_tool = lambda t: tools.append(t)  # type: ignore[method-assign]
        register_conversation_tools(agent, tool_context=None)

        get_tool = next(t for t in tools if t.metadata.name == "get_conversation_messages")
        result = await _call_tool(get_tool, conversation_id="alice@example.com")
        assert "[User] hello from alice" in result
        assert "[Assistant] hi alice" in result

    @pytest.mark.asyncio
    async def test_falls_back_to_tool_context_current_chat(self):
        agent = _make_agent()
        await agent.observe("group message", conversation_id="120363@g.us")

        ctx = ToolContext(chat_id="120363@g.us")
        tools: list = []
        agent.register_tool = lambda t: tools.append(t)  # type: ignore[method-assign]
        register_conversation_tools(agent, tool_context=ctx)

        get_tool = next(t for t in tools if t.metadata.name == "get_conversation_messages")
        result = await _call_tool(get_tool, conversation_id="")
        assert "group message" in result

    @pytest.mark.asyncio
    async def test_lists_all_conversations_when_no_id_and_no_context(self):
        agent = _make_agent()
        await agent.observe("hello from alice", conversation_id="alice@example.com")
        await agent.record_assistant_message("bob@example.com", "hi bob")

        tools: list = []
        agent.register_tool = lambda t: tools.append(t)  # type: ignore[method-assign]
        register_conversation_tools(agent, tool_context=None)

        get_tool = next(t for t in tools if t.metadata.name == "get_conversation_messages")
        result = await _call_tool(get_tool, conversation_id="")
        assert "=== alice@example.com ===" in result
        assert "hello from alice" in result
        assert "=== bob@example.com ===" in result
        assert "hi bob" in result

    @pytest.mark.asyncio
    async def test_no_id_and_no_history_returns_no_conversations_found(self):
        agent = _make_agent()
        tools: list = []
        agent.register_tool = lambda t: tools.append(t)  # type: ignore[method-assign]
        register_conversation_tools(agent, tool_context=None)

        get_tool = next(t for t in tools if t.metadata.name == "get_conversation_messages")
        result = await _call_tool(get_tool, conversation_id="")
        assert result == "No conversations found."


class TestRecordNote:
    @pytest.mark.asyncio
    async def test_writes_note_prefixed_message(self):
        agent = _make_agent()
        tools: list = []
        agent.register_tool = lambda t: tools.append(t)  # type: ignore[method-assign]
        register_conversation_tools(agent, tool_context=None)

        note_tool = next(t for t in tools if t.metadata.name == "record_note")
        note = "gina is on Premium tier"
        conversation = "gina@example.com"
        result = await _call_tool(note_tool, note=note, conversation_id=conversation)
        assert "note recorded" in result

        history = agent.get_conversation_history(conversation)
        assert len(history) == 1
        assert history[0].content
        assert history[0].content.startswith(_NOTE_PREFIX)
        assert "gina is on Premium tier" in history[0].content
        assert history[0].role == MessageRole.ASSISTANT

    @pytest.mark.asyncio
    async def test_note_appears_in_future_turns(self):
        agent = _make_agent()
        tools: list = []
        agent.register_tool = lambda t: tools.append(t)  # type: ignore[method-assign]
        register_conversation_tools(agent, tool_context=None)

        note_tool = next(t for t in tools if t.metadata.name == "record_note")
        conversation = "hank@example.com"
        await _call_tool(note_tool, note="prefers short answers", conversation_id=conversation)

        # Simulate a subsequent inbound turn loading hank's history
        history = agent.get_conversation_history("hank@example.com")
        assert any(_NOTE_PREFIX in (m.content or "") for m in history)

    @pytest.mark.asyncio
    async def test_error_when_no_conversation_id_and_no_context(self):
        agent = _make_agent()
        tools: list = []
        agent.register_tool = lambda t: tools.append(t)  # type: ignore[method-assign]
        register_conversation_tools(agent, tool_context=None)

        note_tool = next(t for t in tools if t.metadata.name == "record_note")
        result = await _call_tool(note_tool, note="some note", conversation_id="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_error_when_note_is_empty(self):
        agent = _make_agent()
        tools: list = []
        agent.register_tool = lambda t: tools.append(t)  # type: ignore[method-assign]
        register_conversation_tools(agent, tool_context=None)

        note_tool = next(t for t in tools if t.metadata.name == "record_note")
        result = await _call_tool(note_tool, note="", conversation_id="gina@example.com")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_falls_back_to_tool_context_current_chat(self):
        agent = _make_agent()
        ctx = ToolContext(chat_id="120363@g.us")
        tools: list = []
        agent.register_tool = lambda t: tools.append(t)  # type: ignore[method-assign]
        register_conversation_tools(agent, tool_context=ctx)

        note_tool = next(t for t in tools if t.metadata.name == "record_note")
        result = await _call_tool(note_tool, note="group prefers short answers", conversation_id="")
        assert "120363@g.us" in result

        history = agent.get_conversation_history("120363@g.us")
        assert any(_NOTE_PREFIX in (m.content or "") for m in history)


class TestGetConversationHistory:
    """Test the public method on KaiAgent that the tools wrap."""

    @pytest.mark.asyncio
    async def test_returns_copy_not_reference(self):
        agent = _make_agent()
        await agent.observe("message", conversation_id="chat-1")

        history = agent.get_conversation_history("chat-1")
        assert len(history) == 1
        # Mutating the returned list must not affect the agent's store
        history.clear()
        assert len(agent.get_conversation_history("chat-1")) == 1

    @pytest.mark.asyncio
    async def test_returns_empty_for_unknown_conversation(self):
        agent = _make_agent()
        assert agent.get_conversation_history("never-seen") == []
