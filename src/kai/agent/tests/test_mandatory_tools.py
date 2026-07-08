"""Tests for mandatory-Brain steering.

The Brain ``mandatory`` flag is *strong steering*, not a code-level guarantee.
Two mechanisms implement it:

1. The workflow prompt (``build_brain_workflow_instruction`` with
   ``mandatory=True``) tells the model it MUST call ``brain_query`` first,
   fall back to ``web_search`` when the Brain has nothing, and never answer
   facts from training data.
2. ``cli/bot.py`` lowers the LLM temperature (greedy decoding) to raise the
   probability the model follows that instruction.

These tests cover the prompt builder and confirm the agent no longer performs
any post-turn retry (the removed "hard guarantee" that was really a nudge).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict

from llama_index.core.llms import ChatMessage, MessageRole

from kai.agent.core import ActionResult, KaiAgent, ToolCallRecord
from kai.agent.goal import GoalManager
from kai.brain.config import BRAIN_TOOL_NAME, build_brain_workflow_instruction


class _StubAgent(KaiAgent):
    """A KaiAgent that skips the real __init__ (no LLM, no tools, no files)."""

    def __init__(self) -> None:  # noqa: D401 - test double
        self._system_prompt: str | None = None
        self._save_lock = asyncio.Lock()
        self._history: OrderedDict[str, list] = OrderedDict()
        self._timestamps: OrderedDict[str, list] = OrderedDict()
        self.goal_manager = GoalManager()
        self._temperature: float | None = None
        self._history_file = None
        self._goal_file = None
        self._namespace = ""
        self._run_calls: list[list] = []  # the messages list passed each call
        self._scripted: list[tuple[ActionResult, list[ToolCallRecord]]] = []

    # --- stubs for everything chat() touches besides _run_with_tools ---
    def _format_user_message(self, message, context=None):
        return message

    def _build_messages(
        self,
        user_message,
        conversation_id=None,
        system_prompt=None,
        images=None,
        videos=None,
        extra_system_context=None,
        reply_style=None,
    ) -> list[ChatMessage]:
        return [ChatMessage(role=MessageRole.USER, content=user_message)]

    def _history_key(self, conversation_id=None):
        return conversation_id or "default"

    def _get_history(self, conversation_id=None):
        return self._history.setdefault(self._history_key(conversation_id), [])

    def _trim_history(self, conversation_id=None):
        pass

    def _mark_dirty(self):
        pass

    def _save_goal(self):
        pass

    # --- the thing we want to observe ---
    async def _run_with_tools(self, messages, output_cls, tools=None):
        self._run_calls.append(messages)
        return self._scripted.pop(0)


def _action(text: str | None, action: str = "reply") -> ActionResult:
    return ActionResult(action=action, text=text)


class TestNoRetry:
    """chat() runs the tool loop exactly once — there is no post-turn retry."""

    async def test_missing_brain_does_not_trigger_retry(self):
        agent = _StubAgent()
        # Model answers from memory, never calls brain_query. With steering
        # (not enforcement) there is no second turn.
        agent._scripted = [(_action("answer from memory"), [])]
        await agent.chat("q", output_cls=ActionResult)
        assert len(agent._run_calls) == 1

    async def test_temperature_override_is_passed_through(self):
        agent = _StubAgent()
        agent.set_temperature(0.0)
        assert agent._temperature == 0.0
        agent.set_temperature(None)
        assert agent._temperature is None


class TestMandatoryWorkflowPrompt:
    """The mandatory prompt steers grounding: Brain first, web fallback, else decline."""

    def test_non_mandatory_has_no_fallback_rule(self):
        prompt = build_brain_workflow_instruction("", mandatory=False)
        assert BRAIN_TOOL_NAME in prompt
        assert "web_search" not in prompt
        assert "MUST" not in prompt

    def test_mandatory_no_triggers_includes_web_fallback(self):
        prompt = build_brain_workflow_instruction("", mandatory=True)
        assert "MUST" in prompt
        assert BRAIN_TOOL_NAME in prompt
        assert "web_search" in prompt
        assert "training data" in prompt

    def test_mandatory_with_triggers_uses_must_and_fallback(self):
        prompt = build_brain_workflow_instruction(
            "questions about pricing\nrefund policy", mandatory=True
        )
        assert "You MUST call it when:" in prompt
        assert "- questions about pricing" in prompt
        assert "- refund policy" in prompt
        assert "web_search" in prompt

    def test_non_mandatory_with_triggers_uses_should(self):
        prompt = build_brain_workflow_instruction("pricing", mandatory=False)
        assert "You SHOULD call it when:" in prompt
        assert "web_search" not in prompt
