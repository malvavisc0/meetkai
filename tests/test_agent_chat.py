import json

import pytest
import respx
from httpx import Response
from llama_index.core.llms import MessageRole
from llama_index.llms.openai_like import OpenAILike

from kai.agent.core import DEFAULT_SYSTEM_PROMPT, ActionResult, KaiAgent
from kai.agent.goal import GoalManager
from kai.config.settings import Settings
from tests.agent_test_helpers import _mock_llm, _TestAction


class TestKaiAgentChat:
    @pytest.mark.asyncio
    async def test_chat_returns_reply(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent.llm = _mock_llm("Why did the chicken?")

        result = await agent.chat("Tell me a joke", output_cls=_TestAction)
        assert result.action.text == "Why did the chicken?"
        assert result.reply == "Why did the chicken?"

    @pytest.mark.asyncio
    async def test_chat_stores_history(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent.llm = _mock_llm("Sure!")

        await agent.chat("Hello", output_cls=_TestAction)
        history = agent.get_history()
        assert len(history) == 2
        assert history[0].role == MessageRole.USER
        assert history[0].content == "Hello"
        assert history[1].role == MessageRole.ASSISTANT
        assert history[1].content == "Sure!"

    @pytest.mark.asyncio
    async def test_chat_uses_system_prompt_and_history(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent.set_system_prompt("You are a joke bot.")
        agent.llm = _mock_llm("Ha!")

        await agent.chat("first", output_cls=_TestAction)
        await agent.chat("second", output_cls=_TestAction)

        second_call = agent.llm.achat_with_tools.call_args_list[1]
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
        agent.llm = _mock_llm("ok")

        await agent.chat("hello", output_cls=_TestAction, system_prompt="Group: Alice, Bob")

        call_args = agent.llm.achat_with_tools.call_args_list[0]
        messages = call_args.kwargs["chat_history"]
        assert "Group: Alice, Bob" in messages[0].content
        assert "Base prompt." not in messages[0].content

    @pytest.mark.asyncio
    async def test_chat_returns_error_on_failure(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        llm = _mock_llm()
        llm.achat_with_tools.side_effect = RuntimeError("API down")
        agent.llm = llm

        result = await agent.chat("Hello", output_cls=_TestAction)
        assert result.error is not None
        assert result.action.action == "error"

    @pytest.mark.asyncio
    async def test_chat_trims_history(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent._max_history = 4
        agent.llm = _mock_llm("ok")

        for i in range(10):
            await agent.chat(f"msg{i}", output_cls=_TestAction)

        assert len(agent.get_history()) == 4

    @pytest.mark.asyncio
    async def test_chat_isolates_history_by_conversation(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent.llm = _mock_llm("ok")

        await agent.chat("from chat one", output_cls=_TestAction, conversation_id="chat-1")
        await agent.chat("from chat two", output_cls=_TestAction, conversation_id="chat-2")

        second_call = agent.llm.achat_with_tools.call_args_list[1]
        messages = second_call.kwargs["chat_history"]
        assert DEFAULT_SYSTEM_PROMPT in messages[0].content
        assert messages[1].content == "from chat two"

    @pytest.mark.asyncio
    async def test_observe_stores_message_without_reply(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent.llm = _mock_llm("ok")

        await agent.observe("background message", conversation_id="chat-1")
        history = agent.get_history("chat-1")
        assert len(history) == 1
        assert history[0].role == MessageRole.USER
        assert history[0].content == "background message"

    @pytest.mark.asyncio
    async def test_chat_store_user_message_can_be_disabled(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent.llm = _mock_llm("ok")

        await agent.observe("user message", conversation_id="chat-1")
        await agent.chat(
            "user message",
            output_cls=_TestAction,
            conversation_id="chat-1",
            store_user_message=False,
        )
        history = agent.get_history("chat-1")
        assert len(history) == 2
        assert [m.role for m in history] == [MessageRole.USER, MessageRole.ASSISTANT]

    @pytest.mark.asyncio
    async def test_chat_guards_empty_reply(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent.llm = _mock_llm(reply_content=None)

        result = await agent.chat("Hello", output_cls=_TestAction)
        assert result.reply == ""
        assert result.action.text is None
        # An empty reply stores nothing (mirrors the old silent early-return).
        assert len(agent.get_history()) == 0

    @pytest.mark.asyncio
    async def test_chat_delegated_action_stores_user_message_only(self, settings):
        # Regression: an operator turn whose action is delegated must still
        # persist the operator's inbound instruction in the operator history
        # bucket — the assistant reply text is recorded in the *target* chat
        # by the bot's dispatch, not here. Previously the entire history
        # block was skipped when the action was delegated, losing the
        # operator's message.
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent.llm = _mock_llm("hola mundo")

        result = await agent.chat(
            "send a message to the group saying hello",
            output_cls=_TestAction,
            conversation_id="operator",
            is_delegated_action=lambda a: a.action == "reply",
        )

        assert result.action.text == "hola mundo"
        history = agent.get_history("operator")
        # User message IS stored (the operator's instruction).
        assert any(m.role == MessageRole.USER for m in history)
        # Assistant reply is NOT stored here (delegated to target chat).
        assert not any(m.role == MessageRole.ASSISTANT for m in history)

    def test_build_llm_disables_thinking_by_default(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        llm = agent.build_llm()
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
        llm = agent.build_llm()
        assert (
            llm.additional_kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
        )

    def test_build_llm_omits_reasoning_effort_by_default(self, settings):
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        llm = agent.build_llm()
        assert "reasoning_effort" not in llm.additional_kwargs["extra_body"]

    def test_build_llm_sets_reasoning_effort_when_configured(self):
        settings = Settings.for_test(
            llm_api_base="http://localhost:8080/v1",
            llm_api_key="test-key",
            llm_model="test-model",
            agent_history_folder=None,
            llm_reasoning_effort="high",
        )
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        llm = agent.build_llm()
        assert llm.additional_kwargs["extra_body"]["reasoning_effort"] == "high"

    @pytest.mark.asyncio
    async def test_chat_persists_history(self, tmp_path, settings):
        settings.agent_history_folder = tmp_path
        agent = KaiAgent(settings=settings, goal_manager=GoalManager())
        agent.llm = _mock_llm("stored")

        await agent.chat("remember me", output_cls=_TestAction, conversation_id="chat-1")
        await agent.flush()

        restored = KaiAgent(settings=settings, goal_manager=GoalManager())
        assert [message.content for message in restored.get_history("chat-1")] == [
            "remember me",
            "stored",
        ]

    @pytest.mark.asyncio
    async def test_chat_persists_goal_with_history(self, tmp_path, settings):
        settings.agent_history_folder = tmp_path
        gm = GoalManager()
        gm.set_goal("Support users")
        agent = KaiAgent(settings=settings, goal_manager=gm)
        agent.llm = _mock_llm("done")

        await agent.chat("hello", output_cls=_TestAction)
        await agent.flush()

        restored_gm = GoalManager()
        restored = KaiAgent(settings=settings, goal_manager=restored_gm)
        assert restored_gm.has_goal() is True
        restored_goal = restored_gm.get_goal()
        assert restored_goal is not None
        assert restored_goal.description == "Support users"
        assert restored_gm.revision == 0
        assert [message.content for message in restored.get_history("default")] == [
            "hello",
            "done",
        ]


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
        agent.llm = self._real_llm()

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

    def test_parse_structured_text_still_raises_on_malformed(self):
        # Recovery is scoped to a well-formed payload with a disallowed
        # ``action`` value. Genuinely malformed output (no action field, or
        # an action that IS allowed but other fields are broken) must still
        # raise so silent corruption never reaches dispatch.
        from llama_index.core.output_parsers.pydantic import PydanticOutputParser

        parser = PydanticOutputParser(output_cls=_TestAction)
        # No JSON object at all.
        with pytest.raises(ValueError):
            KaiAgent.parse_structured_text("just some prose", _TestAction, parser)
        # JSON object missing the action key.
        with pytest.raises(ValueError):
            KaiAgent.parse_structured_text(json.dumps({"text": "hi"}), _TestAction, parser)

    def test_parse_structured_text_does_not_recover_control_action(self):
        # A no-silent turn excludes ``silent`` from its vocabulary. If the
        # model emits ``silent`` anyway, recovery must NOT kick in: returning
        # silent would ghost the user on a turn designed to forbid silence
        # and bypass the bot's error-retry safety net. The parse must raise
        # so the bot's error path (retry-nudge on direct address) runs.
        from typing import Literal as _Literal

        from llama_index.core.output_parsers.pydantic import PydanticOutputParser

        class _TestNoSilentAction(ActionResult):
            action: _Literal["reply", "console"]  # type: ignore[assignment]
            text: str | None = None

        parser = PydanticOutputParser(output_cls=_TestNoSilentAction)
        # silent with no text (the normal case) -> raises.
        with pytest.raises(ValueError):
            KaiAgent.parse_structured_text(
                json.dumps({"action": "silent"}), _TestNoSilentAction, parser
            )
        # silent WITH text (contradictory) -> still raises; the control-action
        # guard must dominate the "has text" heuristic.
        with pytest.raises(ValueError):
            KaiAgent.parse_structured_text(
                json.dumps({"action": "silent", "text": "ghost me"}),
                _TestNoSilentAction,
                parser,
            )
