from typing import Literal
from unittest.mock import AsyncMock, MagicMock

from llama_index.core.llms import ChatMessage, ChatResponse, MessageRole

from kai.agent.core import ActionResult

_TEST_ACTIONS = Literal["reply", "silent", "sleep"]


class _TestAction(ActionResult):
    """A minimal action vocabulary for unit tests (reply | silent | sleep)."""

    action: _TEST_ACTIONS  # type: ignore[assignment]
    text: str | None = None


def _mock_llm(reply_content: str | None = "ok", *, action: _TEST_ACTIONS = "reply"):
    """Create a mock LLM that returns a simple text reply (no tool calls).

    ``reply_content`` becomes both the raw assistant content (for
    ``achat_with_tools``) and ``action.text`` on the structured result. Pass
    ``action="silent"`` with ``reply_content=None`` to simulate a silent turn.

    The structured terminal step is wired through ``llm.achat`` so the mock
    exercises the same call path as production: ``run_with_tools`` calls
    ``self.llm.achat(messages=...)`` once for the terminal step and parses the
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
