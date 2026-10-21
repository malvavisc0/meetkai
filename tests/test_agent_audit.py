from unittest.mock import AsyncMock, MagicMock

import pytest
from llama_index.core.llms import ChatMessage, ChatResponse, MessageRole

from kai.agent.core import KaiAgent
from kai.agent.goal import GoalManager
from tests.agent_test_helpers import _mock_llm, _TestAction, _wire_structured_action


class TestAuditFixes:
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
        agent.llm = llm

        result = await agent.chat("hello", output_cls=_TestAction)
        assert result.action.text == "Here is my reply"

    def test_history_persists_across_prompt_change(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.get_history("chat").append(ChatMessage(role=MessageRole.USER, content="saved"))
        agent.set_system_prompt("Different prompt")
        history = agent.get_conversation_history("chat")
        assert len(history) == 1
        assert history[0].content == "saved"

    def test_goal_change_preserves_history(self):
        gm = GoalManager()
        agent = KaiAgent(settings=None, goal_manager=gm)
        agent.get_history("chat").append(ChatMessage(role=MessageRole.USER, content="kept"))
        gm.set_goal("New goal")
        agent.build_messages("Hello", conversation_id="chat")
        assert agent.get_conversation_history("chat")[0].content == "kept"

    @pytest.mark.asyncio
    async def test_goal_persisted_separately(self, tmp_path, settings):
        settings.agent_history_folder = tmp_path
        history_dir = tmp_path / "default.history"
        gm = GoalManager()
        gm.set_goal("Test goal")
        agent = KaiAgent(settings=settings, goal_manager=gm)
        agent.llm = _mock_llm("ok")

        await agent.chat("hello", output_cls=_TestAction)
        await agent.flush()

        goal_file = tmp_path / "default.json.goal"
        assert goal_file.exists()

        import json

        data = json.loads(goal_file.read_text())
        assert data["goal"] == "Test goal"

        files = list(history_dir.glob("*.json"))
        assert len(files) == 1
        history_data = json.loads(files[0].read_text())
        assert history_data["key"] == "default"
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

    def test_register_and_unregister_tool_updates_tool_list(self):
        from llama_index.core.tools import FunctionTool

        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        initial_count = len(agent.get_tools())

        def custom_tool(x: str) -> str:
            return x

        tool = FunctionTool.from_defaults(fn=custom_tool, name="custom_test")
        agent.register_tool(tool)
        names = {t.metadata.name for t in agent.get_tools()}
        assert len(agent.get_tools()) == initial_count + 1
        assert "custom_test" in names

        agent.unregister_tool("custom_test")
        names = {t.metadata.name for t in agent.get_tools()}
        assert len(agent.get_tools()) == initial_count
        assert "custom_test" not in names

    def test_clear_tools_drops_defaults(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        assert len(agent.get_tools()) > 0
        agent.clear_tools()
        assert agent.get_tools() == []
        # No tool table is rendered into the prompt after clearing.
        assert "# Tools" not in agent.get_system_prompt()

    def test_add_tool_workflow_appends_to_instructions(self):
        from kai.agent.tools import WEB_WORKFLOW_INSTRUCTIONS

        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        # Default: clean slate, no chat-bot workflow baked in.
        assert "fact-checking" not in agent.get_system_prompt().lower()
        agent.add_tool_workflow(WEB_WORKFLOW_INSTRUCTIONS)
        assert "fact-checking" in agent.get_system_prompt()
        # Turning it back off removes the workflow from the rendered prompt.
        agent.add_tool_workflow(None)
        assert "fact-checking" not in agent.get_system_prompt()

    def test_clear_tools_then_register_keeps_clean_slate(self):
        from llama_index.core.tools import FunctionTool

        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.clear_tools()

        def docker_inspect(container: str) -> str:
            return "ok"

        tool = FunctionTool.from_defaults(fn=docker_inspect, name="docker_inspect")
        agent.register_tool(tool)
        assert len(agent.get_tools()) == 1
        # Only the registered tool appears in the prompt; no web-search table rows.
        prompt = agent.get_system_prompt()
        assert "docker_inspect" in prompt
        assert "web_search" not in prompt

    def test_reply_style_appended(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.set_system_prompt("Base.")
        prompt = agent.get_system_prompt(reply_style="Be very brief.")
        assert "Be very brief." in prompt

    def test_allow_silence_is_a_schema_decision(self):
        # ``allow_silence`` is no longer a runtime prompt flag; "never go
        # silent" is expressed by omitting ``silent`` from the bot's action
        # ``Literal``. The prompt no longer carries a ``<<silent>>`` directive.
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.set_system_prompt("Base.")
        prompt = agent.get_system_prompt()
        assert "<<silent>>" not in prompt
        assert "Never output" not in prompt

    @pytest.mark.asyncio
    async def test_chat_with_reply_style(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent.llm = _mock_llm("ok")
        await agent.chat("hello", output_cls=_TestAction, reply_style="Max 1 sentence.")
        call_args = agent.llm.achat_with_tools.call_args_list[0]
        messages = call_args.kwargs["chat_history"]
        assert "Max 1 sentence." in messages[0].content

    @pytest.mark.asyncio
    async def test_observe_skips_empty(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent.llm = _mock_llm("ok")
        await agent.observe("", conversation_id="test")
        history = agent.get_history("test")
        assert len(history) == 0
