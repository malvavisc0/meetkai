from __future__ import annotations

import json
import logging
from functools import cache
from typing import TYPE_CHECKING, Any

from llama_index.core.llms import ChatMessage, MessageRole
from llama_index.core.tools import FunctionTool
from llama_index.llms.openai_like import OpenAILike

from kai.agent.action_parsing import (
    action_values,
    normalize_action_text,
    parse_kv_lines,
    repair_json,
    strip_reasoning_channels,
)

if TYPE_CHECKING:
    from kai.agent.core import ActionResult, ToolCallCallback, ToolCallRecord

# Upper bound on tool-calling rounds per turn. Sized for the
# fact-checking workflow: one web_search round, then visiting several
# results one-per-round (some live-score/aggregator pages 403/406 and
# must be retried against the next result), plus a final synthesis
# round. The loop exits early as soon as the model stops calling tools.
MAX_TOOL_ROUNDS = 24

logger = logging.getLogger(__name__)


class ToolLoopMixin:
    """Tool-calling loop and structured-action resolution for ``KaiAgent``."""

    _tools: list[FunctionTool]
    _tools_by_name: dict[str | None, FunctionTool]
    _temperature: float | None
    llm: OpenAILike
    _tool_call_callback: ToolCallCallback | None

    async def run_with_tools(
        self,
        messages: list[ChatMessage],
        output_cls: type[ActionResult],
        tools: list[FunctionTool] | None = None,
    ) -> tuple[ActionResult, list[ToolCallRecord]]:
        """Run the tool-calling loop, then resolve a typed ``output_cls`` action.

        Returns ``(action, tool_calls)`` where ``action`` is an instance of
        the bot-declared ``output_cls`` and ``tool_calls`` records every
        side-effecting tool invocation. The action schema is included in the
        loop prompt, so a compliant final message is parsed directly; a
        separate JSON-mode ``self.llm.achat`` call is only made when that
        parse fails.
        """
        active_tools = tools if tools is not None else self._tools
        active_by_name = {t.metadata.name: t for t in active_tools} if tools is not None else None
        scratchpad: list[ChatMessage] = []
        tool_call_records: list[ToolCallRecord] = []
        allowed_values = action_values(output_cls)
        # Temperature override makes the model more deterministic when e.g.
        # Brain is mandatory — greedy decoding increases the chance the model
        # follows MUST instructions.
        llm_kwargs: dict = {}
        if self._temperature is not None:
            llm_kwargs["temperature"] = self._temperature

        from llama_index.core.output_parsers.pydantic import PydanticOutputParser

        json_instruction = self.structured_output_instruction(output_cls)
        loop_messages = self.build_predict_messages(messages, [], json_instruction)
        parser = PydanticOutputParser(output_cls=output_cls)
        final_text: str | None = None

        for _ in range(MAX_TOOL_ROUNDS):
            response = await self.llm.achat_with_tools(
                tools=active_tools,
                chat_history=[*loop_messages, *scratchpad],
                **llm_kwargs,
            )

            tool_calls = self.llm.get_tool_calls_from_response(
                response, error_on_no_tool_call=False
            )

            scratchpad.append(response.message)

            if not tool_calls:
                final_text = response.message.content
                break

            for tc in tool_calls:
                action = self.maybe_resolve_action_tool_call(tc, output_cls, allowed_values)
                if action is not None:
                    return action, tool_call_records
                result, ok = await self.execute_tool_call(tc, active_by_name)
                self.record_tool_call(tc, result, ok, tool_call_records, scratchpad)

        action = self.parse_final_action(final_text, output_cls, parser)
        if action is None:
            action = await self.resolve_terminal_action(
                messages, scratchpad, output_cls, llm_kwargs
            )
        return normalize_action_text(action), tool_call_records

    def parse_final_action(
        self,
        final_text: str | None,
        output_cls: type[ActionResult],
        parser: Any,
    ) -> ActionResult | None:
        """Parse the loop's final message as the terminal action when valid."""
        if not isinstance(final_text, str) or not final_text:
            return None
        try:
            raw_text = strip_reasoning_channels(final_text)
            if not raw_text.strip():
                return None
            return self.parse_structured_text(raw_text, output_cls, parser)
        except Exception:
            logger.debug("Final loop message was not a structured action")
            return None

    def maybe_resolve_action_tool_call(
        self,
        tc: Any,
        output_cls: type[ActionResult],
        action_values: frozenset[str],
    ) -> ActionResult | None:
        """If the model called an action-value name as a tool, resolve it."""
        if tc.tool_name not in action_values or tc.tool_name in self._tools_by_name:
            return None
        action = self.action_from_tool_call(output_cls, tc.tool_name, tc.tool_kwargs)
        if action is not None:
            logger.info("Resolved action-as-tool: %s(%s)", tc.tool_name, tc.tool_kwargs)
            return action
        return None

    async def execute_tool_call(
        self, tc: Any, active_by_name: dict[str | None, FunctionTool] | None
    ) -> tuple[str, bool]:
        """Execute one model tool call and return its result and success flag."""
        tool = self.resolve_tool(tc.tool_name, active_by_name)
        if tool is None:
            return f"Error: unknown tool '{tc.tool_name}'", False
        try:
            output = await tool.acall(**tc.tool_kwargs)
            return str(output.content), True
        except Exception as exc:
            logger.warning("Tool %s failed: %s", tc.tool_name, exc)
            return f"Error calling {tc.tool_name}: {exc}", False

    def resolve_tool(
        self, name: str, active_by_name: dict[str | None, FunctionTool] | None
    ) -> FunctionTool | None:
        """Return the tool named name from the active or default set."""
        if active_by_name is not None:
            found = active_by_name.get(name)
            if found is not None:
                return found
            return self._tools_by_name.get(name)
        return self._tools_by_name.get(name)

    def record_tool_call(
        self,
        tc: Any,
        result: str,
        ok: bool,
        tool_call_records: list[ToolCallRecord],
        scratchpad: list[ChatMessage],
    ) -> None:
        """Record a tool call, fire its callback, and add its scratchpad result."""
        from kai.agent.core import ToolCallRecord

        logger.info("Tool call: %s(%s)", tc.tool_name, tc.tool_kwargs)
        logger.info(
            "Tool result: %s -> %s (%d chars)",
            tc.tool_name,
            "ok" if ok else "error",
            len(result),
        )
        tool_call_records.append(
            ToolCallRecord(name=tc.tool_name, args=dict(tc.tool_kwargs), ok=ok, result=result)
        )
        if self._tool_call_callback is not None:
            try:
                self._tool_call_callback(tc.tool_name, dict(tc.tool_kwargs), result)
            except Exception:
                logger.debug("tool_call_callback raised", exc_info=True)
        scratchpad.append(
            ChatMessage(
                role=MessageRole.TOOL,
                content=result,
                additional_kwargs={"tool_call_id": tc.tool_id},
            )
        )

    @staticmethod
    @cache
    def structured_output_instruction(output_cls: type[ActionResult]) -> str:
        """Return the mandatory JSON output instruction for an action schema."""
        schema = json.dumps(
            output_cls.model_json_schema(), ensure_ascii=False, separators=(",", ":")
        )
        return (
            "\n\n## Output format (mandatory)\n"
            "Respond with ONLY a single JSON object matching this schema — "
            "no prose, no markdown, no code fences, no `key: value` lines. "
            "Choose the action and fill its fields; the whole response must "
            "be that one JSON object.\n\n"
            f"Schema: {schema}"
        )

    async def resolve_terminal_action(
        self,
        messages: list[ChatMessage],
        scratchpad: list[ChatMessage],
        output_cls: type[ActionResult],
        llm_kwargs: dict,
    ) -> ActionResult:
        """Resolve the turn's terminal typed action from the model."""
        from llama_index.core.output_parsers.pydantic import PydanticOutputParser

        parser = PydanticOutputParser(output_cls=output_cls)
        json_instruction = self.structured_output_instruction(output_cls)
        predict_messages = self.build_predict_messages(messages, scratchpad, json_instruction)

        for attempt in range(2):
            try:
                response = await self.llm.achat(messages=predict_messages, **llm_kwargs)
            except Exception as exc:
                logger.warning("Structured prediction LLM call failed: %s", exc)
                raise

            raw_text = strip_reasoning_channels(response.message.content or "")
            try:
                return self.parse_structured_text(raw_text, output_cls, parser)
            except Exception as exc:
                if attempt == 1:
                    logger.warning("Structured output parse failed after retry: %s", exc)
                    raise
                logger.warning("Structured output parse failed, retrying with correction: %s", exc)
                predict_messages = [
                    *predict_messages,
                    ChatMessage(role=MessageRole.ASSISTANT, content=raw_text),
                    ChatMessage(
                        role=MessageRole.SYSTEM,
                        content=(
                            "The previous response was not valid JSON. "
                            "Return ONLY a JSON object matching the schema — "
                            "no prose, no code fences, no field labels."
                        ),
                    ),
                ]
        raise RuntimeError("structured output parsing returned None unexpectedly")

    @staticmethod
    def build_predict_messages(
        messages: list[ChatMessage], scratchpad: list[ChatMessage], json_instruction: str
    ) -> list[ChatMessage]:
        """Build prompt messages for the final structured prediction."""
        sys_msg = messages[0]
        if sys_msg.role == MessageRole.SYSTEM:
            augmented = ChatMessage(
                role=MessageRole.SYSTEM, content=(sys_msg.content or "") + json_instruction
            )
            return [augmented, *messages[1:], *scratchpad]
        return [
            ChatMessage(role=MessageRole.SYSTEM, content=json_instruction),
            *messages,
            *scratchpad,
        ]

    @staticmethod
    def parse_structured_text(
        text: str,
        output_cls: type[ActionResult],
        parser: Any,
    ) -> ActionResult:
        """Parse a model reply into ``output_cls`` via 4 strategies:
        standard JSON extraction, ``key: value`` line parsing, JSON repair,
        and base-class ``ActionResult`` recovery for out-of-scope actions.
        """
        # 1. Standard JSON extraction
        try:
            result = parser.parse(text)
            if isinstance(result, output_cls):
                return normalize_action_text(result)
            return normalize_action_text(output_cls.model_validate(result.model_dump()))
        except Exception:
            pass

        # 2. key: value line format
        kv = parse_kv_lines(text)
        if kv:
            try:
                return normalize_action_text(output_cls.model_validate(kv))
            except Exception:
                pass

        # 3. JSON repair
        repaired = repair_json(text)
        if repaired:
            try:
                return normalize_action_text(output_cls.model_validate_json(repaired))
            except Exception:
                pass

        # 4. Base-class recovery for out-of-scope actions.
        action = ToolLoopMixin.recover_base_action(output_cls, repaired, kv)
        if action is not None:
            return normalize_action_text(action)

        raise ValueError(f"Could not parse structured output from: {text[:200]!r}")

    @staticmethod
    def recover_base_action(
        output_cls: type[ActionResult],
        repaired: str | None,
        kv: dict[str, Any] | None,
    ) -> ActionResult | None:
        """Recover a valid base action from out-of-scope structured output."""
        # Control actions (silent / sleep / console) are excluded — recovering
        # them would ghost the user. A payload failing for any other reason
        # continues to raise so malformed output isn't silently accepted.
        from kai.agent.core import ActionResult

        control_actions = frozenset({"silent", "sleep", "console"})
        allowed = action_values(output_cls)
        payload = ToolLoopMixin.extract_payload(repaired, kv)
        if not (
            isinstance(payload, dict)
            and "action" in payload
            and allowed
            and str(payload["action"]) not in allowed
        ):
            return None
        try:
            base = ActionResult.model_validate(payload)
        except Exception:
            return None
        if base is None or base.action in control_actions or not bool(base.text):
            return None
        logger.warning(
            "Structured output: action %r is not in the %s "
            "vocabulary for this turn; recovering via base "
            "ActionResult so dispatch can degrade gracefully "
            "(e.g. falling back to a plain text reply).",
            base.action,
            output_cls.__name__,
        )
        return base

    @staticmethod
    def extract_payload(repaired: str | None, kv: dict[str, Any] | None) -> dict[str, Any] | None:
        """Extract a JSON payload from repaired text or key/value lines."""
        if repaired:
            try:
                return json.loads(repaired)
            except Exception:
                pass
        return kv

    @staticmethod
    def action_from_tool_call(
        output_cls: type[ActionResult], action_name: str, tool_kwargs: dict
    ) -> ActionResult | None:
        """Build a typed action from a model "action-as-tool" call.

        When the model emits an action value (e.g. ``send_to_group``) as a
        tool call, its keyword arguments are the action's fields. This maps
        them onto ``output_cls`` and strips reasoning-channel artifacts from
        any ``text``. Returns ``None`` if the kwargs don't validate.
        """
        try:
            payload = {**tool_kwargs, "action": action_name}
            action = output_cls.model_validate(payload)
            return normalize_action_text(action)
        except Exception:
            return None
