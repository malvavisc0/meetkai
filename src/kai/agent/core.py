import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from llama_index.core.base.llms.types import ImageBlock, TextBlock, VideoBlock
from llama_index.core.llms import ChatMessage, MessageRole
from llama_index.core.tools import FunctionTool
from llama_index.core.types import PydanticProgramMode
from llama_index.llms.openai_like import OpenAILike
from pydantic import BaseModel, ConfigDict, Field

import kai.agent.openai_compat  # noqa: F401 - installs the VideoBlock serializer shim
from kai.agent.action_parsing import (
    strip_reasoning_channels as strip_reasoning_channels,
)
from kai.agent.action_parsing import (
    unescape_literal_newlines as unescape_literal_newlines,
)
from kai.agent.context import MessageContext
from kai.agent.goal import GoalManager
from kai.agent.history_store import HistoryStoreMixin
from kai.agent.tool_loop import ToolLoopMixin
from kai.agent.tools import get_tool_instructions, get_tools
from kai.agent.tools.escalate import EscalationState
from kai.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# Invoked on every tool call so a caller (e.g. a bot) can render tool usage
# live. Receives (tool_name, tool_kwargs, result); the result string may be
# empty for in-flight calls (before the tool returns).
ToolCallCallback = Callable[[str, dict, str], None]

DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."


class ToolCallRecord(BaseModel):
    """One tool call made by the agent during a turn (side-effecting tools)."""

    model_config = ConfigDict(frozen=True)

    name: str
    args: dict
    ok: bool
    result: str


class ActionResult(BaseModel):
    """Base schema every bot-specific action model must subclass.

    Core only ever touches ``action``/``text``; it never inspects a
    subclass's extra fields (target chat, severity, plan tier, ...) — those
    belong to the bot. Every ``chat()`` turn ends by having the model fill
    one of these, selecting ``action`` from whatever ``Literal`` the bot
    declared on its ``ActionResult`` subclass.
    """

    model_config = ConfigDict(extra="allow")

    action: str  # e.g. "reply" | "silent" | "sleep"
    text: str | None = None  # what to say, if this action says anything
    target: str | None = None  # destination for actions like send_dm / send_to_group


class ChatResult(BaseModel):
    """Structured result of one ``chat()`` turn.

    ``action`` is ALWAYS present — the model's typed decision for this turn.
    ``reply`` is a convenience alias for ``action.text or ""`` kept for
    callers that only want text. ``tool_calls`` are the side-effecting tool
    invocations recorded during the turn.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    reply: str = ""
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    action: ActionResult
    error: str | None = None  # None on success; set on failure


class KaiAgent(HistoryStoreMixin, ToolLoopMixin):
    def __init__(
        self,
        settings: Settings | None = None,
        goal_manager: GoalManager | None = None,
        namespace: str | None = None,
        escalations: EscalationState | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.goal_manager = goal_manager or GoalManager()
        self._namespace = namespace or ""
        self._system_prompt: str | None = None
        self._history_file = self.resolve_history_file()
        self._goal_file: Path | None = (
            Path(f"{self._history_file}.goal") if self._history_file else None
        )
        self._max_history: int = self.settings.agent_max_history_messages
        self._max_history_chars: int = self.settings.agent_max_history_chars
        self._max_conversations: int = self.settings.agent_max_conversations
        self._save_lock: asyncio.Lock = asyncio.Lock()
        # Escalation wiring shared with the owning bot (see BaseBot). A bare
        # agent (no bot, e.g. tests/scripts) gets a fresh in-memory state.
        self.escalations = escalations or EscalationState()
        self._tools = get_tools(self.escalations)
        self._tools_by_name = {tool.metadata.name: tool for tool in self._tools}
        self._tool_workflows: list[str] = []
        self._tool_instructions = get_tool_instructions(self._tools)
        self._tool_call_callback: ToolCallCallback | None = None
        # A single long-lived httpx.AsyncClient shared with the OpenAI SDK's
        # AsyncOpenAI client (via ``async_http_client=``). The SDK does not
        # take ownership/close it, so ``aclose()`` must be called on shutdown.
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.llm_timeout, connect=10.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        self.llm = self.build_llm(async_http_client=self._http)
        self._history: OrderedDict[str, list[ChatMessage]] = OrderedDict()
        self._timestamps: OrderedDict[str, list[str | None]] = OrderedDict()
        self.load_history_state()
        self.load_goal()
        self._dirty: bool = False
        self._flush_task: asyncio.Task | None = None
        self._timezone: str | None = None
        # Per-call temperature override (see set_temperature). When a Brain is
        # mandatory, bot.py lowers this to steer the model toward calling
        # brain_query first — steering, not a guarantee.
        self._temperature: float | None = None

    def set_system_prompt(self, prompt: str, *, clear_history: bool = False) -> None:
        """Set the system prompt, optionally clearing history when it changes."""
        changed = prompt != self._system_prompt
        self._system_prompt = prompt
        logger.info("System prompt set (%d chars)", len(prompt))
        if changed and clear_history:
            self._history.clear()
            self._timestamps.clear()
            self.save_history()

    def register_tool(self, tool: FunctionTool) -> None:
        """Register or replace a tool by name and refresh tool instructions."""
        name = tool.metadata.name
        self._tools_by_name[name] = tool
        self._tools = [t for t in self._tools if t.metadata.name != name]
        self._tools.append(tool)
        self._tool_instructions = get_tool_instructions(
            self._tools, workflow_preamble=self.tool_workflow
        )

    def unregister_tool(self, name: str) -> None:
        """Remove a tool by name and refresh the tool instructions."""
        self._tools_by_name.pop(name, None)
        self._tools = [t for t in self._tools if t.metadata.name != name]
        self._tool_instructions = get_tool_instructions(
            self._tools, workflow_preamble=self.tool_workflow
        )

    def clear_tools(self) -> None:
        """Drop all registered tools (defaults + bot-added).

        A non-chat bot (e.g. a Docker watchdog) calls this in ``configure()``
        before registering its own capabilities, so the agent starts from a
        clean slate without the default web-search/calculator/weather tools
        that only make sense for a chat persona.
        """
        self._tools = []
        self._tools_by_name = {}
        self._tool_instructions = get_tool_instructions(
            self._tools, workflow_preamble=self.tool_workflow
        )

    def add_tool_workflow(self, workflow: str | None) -> None:
        """Add an optional tool-usage guidance block to the system prompt.

        Multiple calls compose (deduplicated). Pass ``None`` to clear all
        workflow blocks back to the tool-table-only prompt.
        """
        if workflow is None:
            self._tool_workflows = []
        elif workflow not in self._tool_workflows:
            self._tool_workflows.append(workflow)
        self._tool_instructions = get_tool_instructions(
            self._tools, workflow_preamble=self.tool_workflow
        )

    @property
    def tool_workflow(self) -> str | None:
        """The combined workflow preamble from all added blocks, or ``None``."""
        if not self._tool_workflows:
            return None
        return "\n\n".join(self._tool_workflows)

    def get_tools(self) -> list:
        """Return a copy of the agent's registered tools."""
        return list(self._tools)

    @property
    def timezone(self) -> str | None:
        """IANA timezone set via ``set_timezone`` (None = process local)."""
        return self._timezone

    def set_timezone(self, timezone: str | None) -> None:
        """Set the timezone used for the "current time" in system prompts.

        Accepts an IANA timezone name (e.g. ``America/Santo_Domingo``). When
        ``None`` (the default), the server process's local timezone is used,
        which is typically UTC in containerized deployments. Bots set this to
        the deployment's local timezone so the model answers in local time
        rather than UTC.
        """
        self._timezone = timezone.strip() if timezone else None

    def set_temperature(self, temperature: float | None) -> None:
        """Override the LLM temperature for all subsequent calls.

        When ``None`` (the default), the LLM uses its built-in default.
        Set to a lower value (e.g. 0.0) to make the model more deterministic.
        Used when a Brain is mandatory to steer the model toward calling
        brain_query first.
        """
        self._temperature = temperature

    def set_tool_call_callback(self, callback: ToolCallCallback | None) -> None:
        """Register a callback fired whenever the agent calls a tool.

        The callback receives ``(tool_name, tool_kwargs, result)`` and is
        intended for live UI rendering (e.g. a bot printing tool usage). It is
        invoked after the tool returns, with the result string.
        """
        self._tool_call_callback = callback

    def build_llm(self, async_http_client: httpx.AsyncClient | None = None) -> OpenAILike:
        """Build the OpenAI-compatible LLM from the agent settings."""
        extra_body: dict[str, Any] = {
            "chat_template_kwargs": {
                "enable_thinking": self.settings.llm_enable_thinking,
            }
        }
        # reasoning_effort is a plain top-level request field on OpenAI-compatible
        # routers (low/medium/high). Only sent when configured so the provider
        # default applies otherwise.
        if self.settings.llm_reasoning_effort:
            extra_body["reasoning_effort"] = self.settings.llm_reasoning_effort
        additional_kwargs = {
            "extra_body": extra_body,
        }
        return OpenAILike(
            model=self.settings.llm_model,
            api_base=self.settings.llm_api_base,
            api_key=self.settings.llm_api_key,
            is_chat_model=True,
            max_tokens=self.settings.llm_max_tokens,
            additional_kwargs=additional_kwargs,
            # llama_index accepts async_http_client at runtime (OpenAI.__init__)
            # but its dataclass-transform stub omits it, hence the ignore.
            async_http_client=async_http_client,  # type: ignore[call-arg]
            is_function_calling_model=True,
            pydantic_program_mode=PydanticProgramMode.LLM,
        )

    def load_goal(self) -> None:
        """Load the persisted goal, if any."""
        if self._goal_file is None or not self._goal_file.exists():
            return
        try:
            raw = json.loads(self._goal_file.read_text(encoding="utf-8"))
            stored_goal = raw.get("goal") if isinstance(raw, dict) else None
            if isinstance(stored_goal, str) and stored_goal:
                self.goal_manager.restore_goal(stored_goal)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Failed to load goal from %s: %s", self._goal_file, exc)

    def save_goal(self) -> None:
        """Persist the current goal, if persistence is enabled."""
        if self._goal_file is None:
            return
        try:
            self._goal_file.parent.mkdir(parents=True, exist_ok=True)
            goal = self.goal_manager.get_goal()
            data = {"goal": goal.description if goal else None}
            temp_path = Path(f"{self._goal_file}.tmp")
            temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(self._goal_file)
        except OSError as exc:
            logger.warning("Failed to save goal to %s: %s", self._goal_file, exc)

    def get_system_prompt(
        self,
        overrides: str | None = None,
        extra_context: str | None = None,
        reply_style: str | None = None,
    ) -> str:
        """Render the system prompt with goal, tools, context, and time."""
        prompt = overrides or self._system_prompt or DEFAULT_SYSTEM_PROMPT
        prompt += self.goal_prompt_section()
        if extra_context:
            prompt += f"\n\n{extra_context}"
        if self._tool_instructions:
            prompt += self._tool_instructions
        prompt += self.time_prompt_section()
        if reply_style:
            prompt += f"\n{reply_style}"
        return prompt

    def goal_prompt_section(self) -> str:
        """Render the current-goal prompt section."""
        if not self.goal_manager.has_goal():
            return ""
        goal = self.goal_manager.get_goal()
        if goal and goal.description:
            return f"\n\nCurrent goal: {goal.description}"
        return ""

    def time_prompt_section(self) -> str:
        """Render the current date and time prompt section."""
        now = datetime.now(UTC)
        try:
            local = now.astimezone(ZoneInfo(self._timezone)) if self._timezone else now.astimezone()
        except (KeyError, ValueError) as exc:
            logger.warning("Unknown timezone %r, falling back to local: %s", self._timezone, exc)
            local = now.astimezone()
        return (
            f"\n\nCurrent date and time: "
            f"{local.strftime('%A, %Y-%m-%d %H:%M:%S %Z')}"
            f" (UTC: {now.strftime('%Y-%m-%d %H:%M:%S')})"
        )

    def build_messages(
        self,
        user_message: str,
        conversation_id: str | None = None,
        system_prompt: str | None = None,
        images: list[bytes] | None = None,
        videos: list[bytes] | None = None,
        extra_system_context: str | None = None,
        reply_style: str | None = None,
    ) -> list[ChatMessage]:
        """Build the system, history, and user messages for a turn."""
        system = self.get_system_prompt(
            overrides=system_prompt,
            extra_context=extra_system_context,
            reply_style=reply_style,
        )
        messages = [ChatMessage(role=MessageRole.SYSTEM, content=system)]
        messages.extend(self.get_history(conversation_id))

        if images or videos:
            blocks: list = [TextBlock(text=user_message)]
            for img_bytes in images or []:
                blocks.append(ImageBlock(image=img_bytes))
            for vid_bytes in videos or []:
                blocks.append(VideoBlock(video=vid_bytes, video_mimetype="video/mp4"))
            messages.append(ChatMessage(role=MessageRole.USER, blocks=blocks))
        else:
            messages.append(ChatMessage(role=MessageRole.USER, content=user_message))
        return messages

    async def observe(
        self,
        message: str,
        conversation_id: str | None = None,
        context: MessageContext | None = None,
        images: list[bytes] | None = None,
        videos: list[bytes] | None = None,
    ) -> None:
        """Store a message in history without generating a reply."""
        if (not message or not message.strip()) and not images and not videos:
            return
        formatted = self.format_user_message(message, context)
        stored_text = self.history_placeholder(formatted, images, videos)
        async with self._save_lock:
            self.get_history(conversation_id).append(
                ChatMessage(role=MessageRole.USER, content=stored_text)
            )
            self._timestamps.setdefault(self.history_key(conversation_id), []).append(self.now_ts())
            self.trim_history(conversation_id)
        self.mark_dirty()

    def history_placeholder(
        self,
        formatted: str,
        images: list[bytes] | None,
        videos: list[bytes] | None = None,
    ) -> str:
        """Append a media summary to a stored message body."""
        media = []
        if images:
            media.append(f"{len(images)} image(s), {sum(len(b) for b in images) // 1024} KB")
        if videos:
            media.append(f"{len(videos)} video(s), {sum(len(b) for b in videos) // 1024} KB")
        if not media:
            return formatted
        return f"{formatted}\n[{', '.join(media)}]"

    async def record_assistant_message(self, conversation_id: str, text: str) -> None:
        """Record an assistant turn in a conversation's history (no LLM call).

        Used by side-effecting tools — e.g. an operator ``send_message`` tool
        that posts to a *different* chat than the one the turn is running in —
        so the target chat sees "kAI: <what it sent>" and retains continuity,
        without a synthetic user turn polluting it.
        """
        if not text or not text.strip():
            return
        key = self.history_key(conversation_id)
        async with self._save_lock:
            self.get_history(conversation_id).append(
                ChatMessage(role=MessageRole.ASSISTANT, content=text)
            )
            self._timestamps.setdefault(key, []).append(self.now_ts())
            self.trim_history(conversation_id)
        self.mark_dirty()

    async def chat(
        self,
        message: str,
        output_cls: type[ActionResult],
        conversation_id: str | None = None,
        context: MessageContext | None = None,
        system_prompt: str | None = None,
        images: list[bytes] | None = None,
        videos: list[bytes] | None = None,
        store_user_message: bool = True,
        extra_system_context: str | None = None,
        reply_style: str | None = None,
        tools: list[FunctionTool] | None = None,
        is_delegated_action: Callable[[ActionResult], bool] | None = None,
    ) -> ChatResult:
        """Run one agent turn and return its structured result."""
        formatted = self.format_user_message(message, context)
        messages = self.build_messages(
            formatted,
            conversation_id=conversation_id,
            system_prompt=system_prompt,
            images=images,
            videos=videos,
            extra_system_context=extra_system_context,
            reply_style=reply_style,
        )

        try:
            action, tool_calls = await self.run_with_tools(
                messages, output_cls=output_cls, tools=tools
            )
            await self.persist_turn(
                formatted,
                conversation_id,
                action,
                store_user_message,
                is_delegated_action,
                images=images,
                videos=videos,
            )
            return ChatResult(reply=action.text or "", tool_calls=tool_calls, action=action)
        except TimeoutError as exc:
            return self.chat_error(
                exc, "timeout", "Sorry, the language model took too long to respond."
            )
        except Exception as exc:
            logger.exception("Agent chat error")
            return self.chat_error(
                exc, "", "Sorry, I encountered an error processing your message."
            )

    async def persist_turn(
        self,
        formatted: str,
        conversation_id: str | None,
        action: ActionResult,
        store_user_message: bool,
        is_delegated_action: Callable[[ActionResult], bool] | None,
        images: list[bytes] | None = None,
        videos: list[bytes] | None = None,
    ) -> None:
        """Persist a completed turn's user and assistant messages."""
        reply_text = action.text or ""

        # ``is_delegated_action`` marks actions whose text targets a
        # different conversation (e.g. ``send_to_group``). Those must not
        # be recorded as an assistant reply in this conversation.
        delegated = is_delegated_action is not None and is_delegated_action(action)

        # Save assistant reply only when it targets this conversation.
        # Save user message on any turn that produced output or was
        # delegated — bare ``silent`` turns store nothing here.
        save_assistant = bool(reply_text) and not delegated
        save_user = store_user_message and (bool(reply_text) or delegated)
        if not (save_user or save_assistant):
            await asyncio.to_thread(self.save_goal)
            return
        async with self._save_lock:
            history = self.get_history(conversation_id)
            key = self.history_key(conversation_id)
            ts_list = self._timestamps.setdefault(key, [])
            if save_user:
                history.append(
                    ChatMessage(
                        role=MessageRole.USER,
                        content=self.history_placeholder(formatted, images, videos),
                    )
                )
                ts_list.append(self.now_ts())
            if save_assistant:
                history.append(ChatMessage(role=MessageRole.ASSISTANT, content=reply_text))
                ts_list.append(self.now_ts())
            self.trim_history(conversation_id)
        self.mark_dirty()
        await asyncio.to_thread(self.save_goal)

    @staticmethod
    def chat_error(exc: Exception, error_tag: str, message: str) -> ChatResult:
        """Build a failed chat result for an exception."""
        if error_tag == "timeout":
            logger.warning("Agent chat timeout: %s", exc)
        return ChatResult(
            reply="",
            tool_calls=[],
            action=ActionResult(action="error", text=message),
            error=f"{error_tag}: {exc}" if error_tag else str(exc),
        )

    async def complete(self, prompt: str) -> str:
        """One-shot non-tool LLM completion, for lightweight checks.

        Used by features that need the model's judgment (e.g. goal-clarity
        checks) without the tool-calling loop or history side effects.
        Returns the raw message content (reasoning channels stripped), or
        an empty string on failure.
        """
        try:
            resp = await self.llm.achat(
                messages=[ChatMessage(role=MessageRole.USER, content=prompt)]
            )
            content = resp.message.content or ""
            return strip_reasoning_channels(content) if isinstance(content, str) else str(content)
        except Exception:
            logger.warning("one-shot LLM completion failed", exc_info=True)
            return ""

    def format_user_message(self, message: str, context: MessageContext | None = None) -> str:
        """Prefix a user message with its speaker header."""
        if context is None:
            return message
        suffix = " (addressing you)" if context.multi_party and context.addressed_to_bot else ""
        header = f"[{context.sender_name}{suffix}]"
        # When the message is a multi-line enrichment block (reply-to / links /
        # image tags stacked above the body), keep the speaker header on
        # its own line so it doesn't fuse with the first metadata tag.
        if "\n" in message:
            return f"{header}\n{message}"
        return f"{header} {message}"

    async def aclose(self) -> None:
        """Release the shared httpx client backing the LLM.

        After this the agent's LLM must not be used again. Call during
        shutdown, after ``flush()``.
        """
        if not self._http.is_closed:
            await self._http.aclose()
