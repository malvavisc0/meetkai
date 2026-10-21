import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from llama_index.core.llms import ChatMessage, ChatResponse, MessageRole
from llama_index.core.tools import FunctionTool

from kai.agent.core import KaiAgent
from kai.agent.goal import GoalManager
from kai.config.settings import Settings
from tests.agent_test_helpers import (
    _mock_llm,
    _TestAction,
    _wire_structured,
    _wire_structured_side_effect,
)


class TestKaiAgentToolHistory:
    @staticmethod
    def _make_tool_call(name="web_search", tool_id="tc_1"):
        tc = MagicMock()
        tc.tool_name = name
        tc.tool_kwargs = {"query": "test"}
        tc.tool_id = tool_id
        return tc

    @staticmethod
    def _stub_tool(name: str, result: object) -> FunctionTool:
        """A tool registered under ``name`` returning ``result`` (or raising it)."""

        async def fn(**kwargs):
            if isinstance(result, Exception):
                raise result
            return result

        return FunctionTool.from_defaults(fn=fn, name=name, description="stub")

    @pytest.mark.asyncio
    async def test_tool_calls_not_in_history(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())

        tc = self._make_tool_call()
        tool_response = ChatResponse(message=ChatMessage(role=MessageRole.ASSISTANT, content=None))
        final_response = ChatResponse(
            message=ChatMessage(role=MessageRole.ASSISTANT, content="Here are the results")
        )

        agent.register_tool(
            self._stub_tool("web_search", "[{'title': 'Result', 'url': 'http://x'}]")
        )

        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(side_effect=[tool_response, final_response])
        mock_llm.get_tool_calls_from_response = MagicMock(side_effect=[[tc], []])
        _wire_structured(mock_llm, "Here are the results")
        agent.llm = mock_llm

        result = await agent.chat("search for test", output_cls=_TestAction)
        assert result.action.text == "Here are the results"

        history = agent.get_history()
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

        agent.register_tool(self._stub_tool("web_search", "result text"))

        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(side_effect=[tool_response, final_response])
        mock_llm.get_tool_calls_from_response = MagicMock(side_effect=[[tc], []])
        _wire_structured(mock_llm, "done")
        agent.llm = mock_llm

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
        agent.register_tool(self._stub_tool("web_search", "x"))
        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(side_effect=[tool_response, final_response])
        mock_llm.get_tool_calls_from_response = MagicMock(side_effect=[[tc], []])
        _wire_structured(mock_llm, "done")
        agent.llm = mock_llm

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
        agent.register_tool(self._stub_tool("web_search", "x"))
        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(side_effect=[tool_response, final_response])
        mock_llm.get_tool_calls_from_response = MagicMock(side_effect=[[tc], []])
        _wire_structured(mock_llm, "done")
        agent.llm = mock_llm

        def bad_callback(name, kwargs, result):
            raise RuntimeError("callback boom")

        agent.set_tool_call_callback(bad_callback)
        # A faulty callback must not break the agent flow.
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
        agent.llm = mock_llm

        result = await agent.chat("do something", output_cls=_TestAction)
        assert result.action.text == "I tried but it failed"

        history = agent.get_history()
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

        agent.register_tool(self._stub_tool("web_search", "[search results]"))
        agent.register_tool(self._stub_tool("get_webpage_html", "Page content"))

        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(side_effect=[round1, round2, final])
        mock_llm.get_tool_calls_from_response = MagicMock(side_effect=[[tc1], [tc2], []])
        _wire_structured(mock_llm, "Combined answer")
        agent.llm = mock_llm

        result = await agent.chat("find info about cats", output_cls=_TestAction)
        assert result.action.text == "Combined answer"

        history = agent.get_history()
        assert len(history) == 2
        assert history[0].role == MessageRole.USER
        assert history[1].role == MessageRole.ASSISTANT
        assert history[1].content == "Combined answer"

    @pytest.mark.asyncio
    async def test_max_tool_rounds_exceeded(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())

        tc = self._make_tool_call()
        tool_response = ChatResponse(message=ChatMessage(role=MessageRole.ASSISTANT, content=None))

        agent.register_tool(self._stub_tool("web_search", "result"))

        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(return_value=tool_response)
        mock_llm.get_tool_calls_from_response = MagicMock(return_value=[tc])
        _wire_structured(mock_llm, "done")
        agent.llm = mock_llm

        result = await agent.chat("infinite loop", output_cls=_TestAction)
        assert result.action.text == "done"

        history = agent.get_history()
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

        agent.register_tool(self._stub_tool("web_search", "Weather: 25°C, sunny"))

        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(side_effect=[tool_response, final_response])
        mock_llm.get_tool_calls_from_response = MagicMock(side_effect=[[tc], []])
        _wire_structured(mock_llm, "It's sunny")
        agent.llm = mock_llm

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
        agent.llm = mock_llm

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

        agent.register_tool(self._stub_tool("web_search", RuntimeError("Connection timeout")))

        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(side_effect=[tool_response, final_response])
        mock_llm.get_tool_calls_from_response = MagicMock(side_effect=[[tc], []])
        _wire_structured(mock_llm, "Had an issue")
        agent.llm = mock_llm

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

        agent.register_tool(self._stub_tool("web_search", "tool result"))

        mock_llm = MagicMock()
        mock_llm.achat_with_tools = AsyncMock(side_effect=[tool_response, final1, plain2])
        mock_llm.get_tool_calls_from_response = MagicMock(side_effect=[[tc], [], []])
        _wire_structured_side_effect(mock_llm, ["answer one", "answer two"])
        agent.llm = mock_llm

        await agent.chat("question one", output_cls=_TestAction)
        await agent.chat("question two", output_cls=_TestAction)

        history = agent.get_history()
        assert len(history) == 4
        assert history[0].role == MessageRole.USER
        assert history[0].content == "question one"
        assert history[1].role == MessageRole.ASSISTANT
        assert history[1].content == "answer one"
        assert history[2].role == MessageRole.USER
        assert history[2].content == "question two"
        assert history[3].role == MessageRole.ASSISTANT
        assert history[3].content == "answer two"


class TestBotHistoryFolderIsolation:
    def test_history_file_resolved_from_folder_and_namespace(self, tmp_path):
        settings = Settings.for_test(
            llm_api_base="http://localhost:8080/v1",
            llm_api_key="test-key",
            agent_history_folder=tmp_path,
        )
        agent = KaiAgent(settings=settings, goal_manager=GoalManager(), namespace="email")
        assert agent.resolve_history_file() == tmp_path / "email.json"

    def test_history_file_default_namespace(self, tmp_path):
        settings = Settings.for_test(
            llm_api_base="http://localhost:8080/v1",
            llm_api_key="test-key",
            agent_history_folder=tmp_path,
        )
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        assert agent.resolve_history_file() == tmp_path / "default.json"

    def test_history_file_none_when_folder_unset(self):
        settings = Settings.for_test(
            llm_api_base="http://localhost:8080/v1",
            llm_api_key="test-key",
            agent_history_folder=None,
        )
        agent = KaiAgent(settings=settings, goal_manager=GoalManager(), namespace="email")
        assert agent.resolve_history_file() is None

    def test_per_bot_history_isolation(self, tmp_path):
        folder = tmp_path / "data"
        settings_primary = Settings.for_test(
            llm_api_base="http://localhost:8080/v1",
            llm_api_key="test-key",
            agent_history_folder=folder,
        )
        settings_email = Settings.for_test(
            llm_api_base="http://localhost:8080/v1",
            llm_api_key="test-key",
            agent_history_folder=folder,
        )

        agent_primary = KaiAgent(
            settings=settings_primary, goal_manager=GoalManager(), namespace="primary"
        )
        agent_email = KaiAgent(
            settings=settings_email, goal_manager=GoalManager(), namespace="email"
        )

        agent_primary._history["chat"] = [ChatMessage(role=MessageRole.USER, content="primary msg")]
        agent_email._history["chat"] = [ChatMessage(role=MessageRole.USER, content="email msg")]

        assert agent_primary.resolve_history_file() == folder / "primary.json"
        assert agent_email.resolve_history_file() == folder / "email.json"
        assert agent_primary._history["chat"][0].content == "primary msg"
        assert agent_email._history["chat"][0].content == "email msg"


class TestHistoryEdits:
    def test_history_key_namespaced_by_bot(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager(), namespace="email")
        assert agent.history_key("123@c.us") == "email:123@c.us"
        assert agent.history_key(None) == "email:default"

    def test_history_key_unnamespaced_without_namespace(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        assert agent.history_key("123@c.us") == "123@c.us"
        assert agent.history_key(None) == "default"

    def test_namespaced_agents_keep_separate_history(self):
        a = KaiAgent(settings=None, goal_manager=GoalManager(), namespace="primary")
        b = KaiAgent(settings=None, goal_manager=GoalManager(), namespace="email")
        a.get_history("shared-id").append(ChatMessage(role=MessageRole.USER, content="primary"))
        b.get_history("shared-id").append(ChatMessage(role=MessageRole.USER, content="email"))
        assert a.get_history("shared-id")[0].content == "primary"
        assert b.get_history("shared-id")[0].content == "email"

    def test_history_conversations_evicted_beyond_cap(self):
        settings = Settings.for_test(agent_max_conversations=3, agent_history_folder=None)
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        for i in range(5):
            agent.get_history(f"chat-{i}").append(
                ChatMessage(role=MessageRole.USER, content=f"msg-{i}")
            )
        conversation_ids = [conversation_id for conversation_id, _, _ in agent.list_conversations()]
        assert len(conversation_ids) == 3
        # oldest conversations evicted (LRU)
        assert "chat-0" not in conversation_ids
        assert "chat-1" not in conversation_ids
        assert "chat-4" in conversation_ids

    @pytest.mark.asyncio
    async def test_debounced_save_flushed_on_flush(self, tmp_path, settings):
        settings.agent_history_folder = tmp_path
        history_file = tmp_path / "default.json"
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent.llm = _mock_llm("stored")

        await agent.chat("remember me", output_cls=_TestAction, conversation_id="chat-1")
        # Before flush, the debounced write may not have landed yet.
        await agent.flush()
        assert history_file.exists()
        data = json.loads(history_file.read_text())
        assert any("remember me" in m["content"] for m in data["history"]["chat-1"])

    @pytest.mark.asyncio
    async def test_observe_empty_with_images_stored(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent.llm = _mock_llm("ok")
        await agent.observe("", conversation_id="test", images=[b"\x89PNG"])
        history = agent.get_history("test")
        assert len(history) == 1
        content = history[0].content
        assert content is not None
        assert "image(s)" in content
