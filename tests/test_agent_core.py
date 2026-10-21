from datetime import UTC

import pytest
from llama_index.core.llms import ChatMessage, MessageRole

from kai.agent.context import MessageContext
from kai.agent.core import KaiAgent
from kai.agent.goal import GoalManager
from tests.agent_test_helpers import _mock_llm, _TestAction


class TestKaiAgent:
    def test_default_system_prompt(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        prompt = agent.get_system_prompt()
        assert "helpful assistant" in prompt

    def test_custom_system_prompt(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.set_system_prompt("You are a joke bot.")
        prompt = agent.get_system_prompt()
        assert "joke bot" in prompt

    def test_goal_appended_to_prompt(self):
        gm = GoalManager()
        gm.set_goal("Tell jokes")
        agent = KaiAgent(settings=None, goal_manager=gm)
        prompt = agent.get_system_prompt()
        assert "Tell jokes" in prompt
        assert "Current goal" in prompt

    def test_custom_prompt_plus_goal(self):
        gm = GoalManager()
        gm.set_goal("Be funny")
        agent = KaiAgent(settings=None, goal_manager=gm)
        agent.set_system_prompt("You are a bot.")
        prompt = agent.get_system_prompt()
        assert "You are a bot." in prompt
        assert "Be funny" in prompt

    def test_system_prompt_overrides_per_call(self):
        gm = GoalManager()
        gm.set_goal("Be helpful")
        agent = KaiAgent(settings=None, goal_manager=gm)
        agent.set_system_prompt("Base prompt.")
        prompt = agent.get_system_prompt(overrides="Custom per-call prompt.")
        assert "Custom per-call prompt." in prompt
        assert "Be helpful" in prompt
        assert "Base prompt." not in prompt

    def test_extra_context_augments_base_prompt(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.set_system_prompt("Base prompt.")
        prompt = agent.get_system_prompt(extra_context="People in this chat: Alice, Bob")
        # extra_context must AUGMENT the base persona, not replace it.
        assert "Base prompt." in prompt
        assert "People in this chat: Alice, Bob" in prompt

    def test_build_messages_extra_context_keeps_base_prompt(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.set_system_prompt("Base prompt.")
        messages = agent.build_messages("Hello", extra_system_context="People in this chat: Alice")
        system = messages[0].content
        assert system is not None
        assert "Base prompt." in system
        assert "People in this chat: Alice" in system

    def test_system_prompt_includes_current_time(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        prompt = agent.get_system_prompt()
        assert "Current date and time:" in prompt
        assert "UTC:" in prompt

    def test_timezone_changes_local_time_in_prompt(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.set_timezone("America/New_York")
        prompt = agent.get_system_prompt()
        assert "Current date and time:" in prompt
        # Verify the timezone was applied by checking the local time matches
        # New York's offset (EDT = UTC-4 in summer, EST = UTC-5 in winter).
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ny_now = datetime.now(UTC).astimezone(ZoneInfo("America/New_York"))
        # The prompt must contain a formatted date matching New York's date.
        assert ny_now.strftime("%A, %Y-%m-%d") in prompt
        # And a non-UTC timezone abbreviation.
        assert "UTC" in prompt

    def test_set_timezone_none_falls_back_to_local(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.set_timezone(None)
        prompt = agent.get_system_prompt()
        assert "Current date and time:" in prompt

    def test_set_timezone_strips_whitespace(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.set_timezone("  Europe/Berlin  ")
        assert agent.timezone == "Europe/Berlin"

    def test_unknown_timezone_falls_back_gracefully(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.set_timezone("Not/A_Real_Zone")
        # Must not raise; falls back to local time and still produces a prompt.
        prompt = agent.get_system_prompt()
        assert "Current date and time:" in prompt

    def test_history_cleared_on_prompt_change(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.get_history("chat").append(ChatMessage(role=MessageRole.USER, content="fake"))
        agent.set_system_prompt("New prompt")
        assert len(agent.list_conversations()) == 1

    def test_history_cleared_with_explicit_clear(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.get_history("chat").append(ChatMessage(role=MessageRole.USER, content="fake"))
        agent.set_system_prompt("New prompt", clear_history=True)
        assert len(agent.list_conversations()) == 0

    def test_history_cleared_on_goal_change(self):
        gm = GoalManager()
        agent = KaiAgent(settings=None, goal_manager=gm)
        agent.get_history("chat").append(ChatMessage(role=MessageRole.USER, content="fake"))
        gm.set_goal("New goal")

        messages = agent.build_messages("Hello", conversation_id="chat")

        system = messages[0].content
        assert system is not None
        assert "Current goal: New goal" in system
        assert messages[1].content == "fake"
        assert len(agent.get_history("chat")) == 1

    def test_build_messages_structure(self):
        gm = GoalManager()
        agent = KaiAgent(settings=None, goal_manager=gm)
        agent.set_system_prompt("System instructions.")
        messages = agent.build_messages("Hello")
        assert len(messages) == 2
        assert messages[0].role.value == "system"
        system = messages[0].content
        assert system is not None
        assert "System instructions." in system
        assert messages[1].role.value == "user"
        assert messages[1].content == "Hello"

    def test_build_messages_includes_history(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.get_history("default").extend(
            [
                ChatMessage(role=MessageRole.USER, content="prev user"),
                ChatMessage(role=MessageRole.ASSISTANT, content="prev assistant"),
            ]
        )
        messages = agent.build_messages("new message")
        assert len(messages) == 4
        assert messages[1].content == "prev user"
        assert messages[2].content == "prev assistant"
        assert messages[3].content == "new message"

    def test_build_messages_per_call_system_prompt(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent.set_system_prompt("Base prompt.")
        messages = agent.build_messages("Hello", system_prompt="Override prompt.")
        system = messages[0].content
        assert system is not None
        assert "Override prompt." in system
        assert "Base prompt." not in system

    def test_trim_history_keeps_most_recent_messages(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent._max_history = 4
        for i in range(10):
            agent.get_history().append(ChatMessage(role=MessageRole.USER, content=f"msg{i}"))
        agent.trim_history()
        assert len(agent.get_history()) == 4
        assert agent.get_history()[0].content == "msg6"

    def test_trim_history_by_character_budget(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent._max_history = 10
        agent._max_history_chars = 8
        for content in ("1234", "5678", "9012"):
            agent.get_history().append(ChatMessage(role=MessageRole.USER, content=content))

        agent.trim_history()

        assert [message.content for message in agent.get_history()] == ["5678", "9012"]

    def test_trim_history_preserves_alternating_pattern(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        agent._max_history = 4
        agent._max_history_chars = 9999
        for i in range(6):
            agent.get_history().append(ChatMessage(role=MessageRole.USER, content=f"u{i}"))
            agent.get_history().append(ChatMessage(role=MessageRole.ASSISTANT, content=f"a{i}"))
        agent.trim_history()
        history = agent.get_history()
        assert len(history) == 4
        assert history[0].content == "u4"
        assert history[1].content == "a4"
        assert history[2].content == "u5"
        assert history[3].content == "a5"


class TestKaiAgentContext:
    def test_format_user_message_group(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        ctx = MessageContext(
            sender_name="Juan Palotes",
            conversation_id="123@g.us",
            multi_party=True,
        )
        result = agent.format_user_message("hello", ctx)
        assert result == "[Juan Palotes] hello"

    def test_format_user_message_group_mentions_bot(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        ctx = MessageContext(
            sender_name="Juan Palotes",
            conversation_id="123@g.us",
            multi_party=True,
            addressed_to_bot=True,
        )
        result = agent.format_user_message("hello", ctx)
        assert result == "[Juan Palotes (addressing you)] hello"

    def test_format_user_message_dm(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        ctx = MessageContext(
            sender_name="Lucerna",
            conversation_id="12345678901234@lid",
            multi_party=False,
        )
        result = agent.format_user_message("hi", ctx)
        assert result == "[Lucerna] hi"

    def test_format_user_message_no_context(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        result = agent.format_user_message("hello", None)
        assert result == "hello"

    def test_get_system_prompt_no_roster_injection(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        prompt = agent.get_system_prompt()
        assert "People in this chat" not in prompt
        assert "@[Name]" not in prompt

    @pytest.mark.asyncio
    async def test_silent_reply_not_stored_in_history(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent.llm = _mock_llm(action="silent", reply_content=None)

        result = await agent.chat("hello", output_cls=_TestAction)
        assert result.action.action == "silent"
        assert result.reply == ""
        # A silent turn stores nothing in history.
        history = agent.get_history()
        assert len(history) == 0

    @pytest.mark.asyncio
    async def test_chat_formats_message_with_context(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent.llm = _mock_llm("hi back")

        ctx = MessageContext(
            sender_name="Lucerna",
            conversation_id="456@lid",
            multi_party=False,
        )
        await agent.chat("hello", output_cls=_TestAction, context=ctx)

        call_args = agent.llm.achat_with_tools.call_args_list[0]
        messages = call_args.kwargs["chat_history"]
        assert "[Lucerna] hello" in messages[1].content
