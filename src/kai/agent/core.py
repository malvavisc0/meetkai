import asyncio
import copy
import json
import logging
import re
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from llama_index.core.base.llms.types import ImageBlock, TextBlock
from llama_index.core.llms import ChatMessage, MessageRole
from llama_index.core.tools import FunctionTool
from llama_index.llms.openai_like import OpenAILike

from kai.agent.context import MessageContext
from kai.agent.goal import GoalManager
from kai.agent.tools import get_tool_instructions, get_tools
from kai.config.settings import Settings, get_settings

# Upper bound on tool-calling rounds per turn. Sized for the
# fact-checking workflow: one web_search round, then visiting several
# results one-per-round (some live-score/aggregator pages 403/406 and
# must be retried against the next result), plus a final synthesis
# round. The loop exits early as soon as the model stops calling tools.
_MAX_TOOL_ROUNDS = 12
_FLUSH_DELAY = 0.5

logger = logging.getLogger(__name__)

# Type of an optional callback invoked when the agent calls a tool, so callers
# (e.g. a bot) can render tool usage live. Receives (tool_name, tool_kwargs,
# result); the result string may be empty for in-flight calls.
ToolCallCallback = Callable[[str, dict, str], None]

_DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
_SILENT_MARKER = "<<silent>>"
# Tolerate the reply being wrapped in backticks (models mirroring code-span
# formatting from prompts) and an optional trailing parenthetical note.
_SILENT_RE = re.compile(r"^\s*`?\s*<<silent>>\s*(?:\(.*\))?\s*`?\s*$")
_VALID_ROLES = frozenset(r.value for r in MessageRole)

# Reasoning models sometimes emit their chain-of-thought wrapped in "channel"
# tokens (e.g. ``<|channel>thought ... <channel|>``). These must never reach
# the user: strip whole channel blocks first, then any stray channel markers.
_REASONING_BLOCK_RE = re.compile(r"<\|?channel\|?>.*?<\|?channel\|?>", re.DOTALL)
_REASONING_TOKEN_RE = re.compile(r"<\|?channel\|?>")


def strip_reasoning_channels(text: str) -> str:
    """Remove reasoning-model "channel" artifacts from model output.

    Strips ``<|channel>...<channel|>`` blocks and any leftover channel
    markers, returning the user-visible content. Returns the input unchanged
    (apart from stripping) if no channel tokens are present.
    """
    if not text:
        return text
    text = _REASONING_BLOCK_RE.sub("", text)
    text = _REASONING_TOKEN_RE.sub("", text)
    return text.strip()


def is_silent_reply(reply: str) -> bool:
    return bool(_SILENT_RE.match(reply.strip()))


class KaiAgent:
    def __init__(
        self,
        settings: Settings | None = None,
        goal_manager: GoalManager | None = None,
        namespace: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.goal_manager = goal_manager or GoalManager()
        self._namespace = namespace or ""
        self._system_prompt: str | None = None
        self._history_file = self._resolve_history_file()
        self._goal_file: Path | None = (
            Path(f"{self._history_file}.goal") if self._history_file else None
        )
        self._max_history: int = self.settings.agent_max_history_messages
        self._max_history_chars: int = self.settings.agent_max_history_chars
        self._max_conversations: int = self.settings.agent_max_conversations
        self._save_lock: asyncio.Lock = asyncio.Lock()
        self._tools = get_tools()
        self._tools_by_name = {tool.metadata.name: tool for tool in self._tools}
        self._tool_workflow: str | None = None
        self._tool_instructions = get_tool_instructions(self._tools)
        self._tool_call_callback: ToolCallCallback | None = None
        self._llm = self._build_llm()
        self._history: OrderedDict[str, list[ChatMessage]] = OrderedDict(self._load_history())
        self._load_goal()
        self._goal_revision = self.goal_manager.revision
        self._dirty: bool = False
        self._flush_task: asyncio.Task | None = None
        self._timezone: str | None = None

    def _resolve_history_file(self) -> Path | None:
        folder = self.settings.agent_history_folder
        if folder is None:
            return None
        name = self._namespace or "default"
        return Path(folder) / f"{name}.json"

    def set_system_prompt(self, prompt: str, *, clear_history: bool = False) -> None:
        changed = prompt != self._system_prompt
        self._system_prompt = prompt
        logger.info("System prompt set (%d chars)", len(prompt))
        if changed and clear_history:
            self._history.clear()
            self._save_history()

    def register_tool(self, tool: FunctionTool) -> None:
        name = tool.metadata.name
        self._tools_by_name[name] = tool
        if not any(t.metadata.name == name for t in self._tools):
            self._tools.append(tool)
        self._tool_instructions = get_tool_instructions(
            self._tools, workflow_preamble=self._tool_workflow
        )

    def unregister_tool(self, name: str) -> None:
        self._tools_by_name.pop(name, None)
        self._tools = [t for t in self._tools if t.metadata.name != name]
        self._tool_instructions = get_tool_instructions(
            self._tools, workflow_preamble=self._tool_workflow
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
            self._tools, workflow_preamble=self._tool_workflow
        )

    def set_tool_workflow(self, workflow: str | None) -> None:
        """Set the optional tool-usage guidance appended to the system prompt.

        ``None`` (the default) keeps the prompt clean — only the generic tool
        table is shown. Pass a workflow block (e.g.
        :data:`WEB_WORKFLOW_INSTRUCTIONS`) to teach the model *how* to use the
        tools; chat bots that expose web search opt into the fact-checking
        workflow, while a Docker ops bot leaves it off.
        """
        self._tool_workflow = workflow
        self._tool_instructions = get_tool_instructions(
            self._tools, workflow_preamble=self._tool_workflow
        )

    def get_tools(self) -> list:
        return list(self._tools)

    def set_timezone(self, timezone: str | None) -> None:
        """Set the timezone used for the "current time" in system prompts.

        Accepts an IANA timezone name (e.g. ``America/Santo_Domingo``). When
        ``None`` (the default), the server process's local timezone is used,
        which is typically UTC in containerized deployments. Bots set this to
        the deployment's local timezone so the model answers in local time
        rather than UTC.
        """
        self._timezone = timezone.strip() if timezone else None

    def set_tool_call_callback(self, callback: ToolCallCallback | None) -> None:
        """Register a callback fired whenever the agent calls a tool.

        The callback receives ``(tool_name, tool_kwargs, result)`` and is
        intended for live UI rendering (e.g. a bot printing tool usage). It is
        invoked after the tool returns, with the result string.
        """
        self._tool_call_callback = callback

    def _build_llm(self) -> OpenAILike:
        additional_kwargs = {
            "extra_body": {
                "chat_template_kwargs": {
                    "enable_thinking": self.settings.llm_enable_thinking,
                }
            }
        }
        return OpenAILike(
            model=self.settings.llm_model,
            api_base=self.settings.llm_api_base,
            api_key=self.settings.llm_api_key,
            is_chat_model=True,
            additional_kwargs=additional_kwargs,
            is_function_calling_model=True,
        )

    def _history_key(self, conversation_id: str | None = None) -> str:
        if not conversation_id:
            logger.debug("conversation_id not provided; using 'default' bucket")
            key = "default"
        else:
            key = conversation_id
        if self._namespace:
            return f"{self._namespace}:{key}"
        return key

    def _load_history(self) -> dict[str, list[ChatMessage]]:
        if self._history_file is None:
            return {}

        try:
            if not self._history_file.exists():
                return {}
            raw = json.loads(self._history_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                logger.warning("Ignoring invalid agent history file: %s", self._history_file)
                return {}

            history_data = raw.get("history", raw)
            if not isinstance(history_data, dict):
                return {}

            history: dict[str, list[ChatMessage]] = {}
            for key, messages in history_data.items():
                if not isinstance(key, str) or not isinstance(messages, list):
                    continue
                history[key] = []
                for item in messages:
                    if not isinstance(item, dict):
                        continue
                    role = item.get("role")
                    content = item.get("content")
                    if role in _VALID_ROLES and isinstance(content, str):
                        history[key].append(ChatMessage(role=MessageRole(role), content=content))
            return history
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Failed to load agent history from %s: %s", self._history_file, exc)
            return {}

    def _load_goal(self) -> None:
        if self._goal_file is None or not self._goal_file.exists():
            return
        try:
            raw = json.loads(self._goal_file.read_text(encoding="utf-8"))
            stored_goal = raw.get("goal") if isinstance(raw, dict) else None
            if isinstance(stored_goal, str) and stored_goal:
                self.goal_manager.restore_goal(stored_goal)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Failed to load goal from %s: %s", self._goal_file, exc)

    def _save_history(self) -> None:
        if self._history_file is None:
            return

        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            snapshot = {
                key: [
                    {"role": message.role.value, "content": message.content or ""}
                    for message in messages
                ]
                for key, messages in self._history.items()
            }
            data = {"history": snapshot}
            temp_path = Path(f"{self._history_file}.tmp")
            temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(self._history_file)
        except OSError as exc:
            logger.warning("Failed to save agent history to %s: %s", self._history_file, exc)

    def _save_goal(self) -> None:
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

    def _sync_goal_state(self) -> None:
        if self._goal_revision == self.goal_manager.revision:
            return
        self._goal_revision = self.goal_manager.revision

    def _get_system_prompt(
        self,
        overrides: str | None = None,
        extra_context: str | None = None,
        reply_style: str | None = None,
        allow_silence: bool = True,
    ) -> str:
        prompt = overrides or self._system_prompt or _DEFAULT_SYSTEM_PROMPT
        if self.goal_manager.has_goal():
            goal = self.goal_manager.get_goal()
            if goal and goal.description:
                prompt += f"\n\nCurrent goal: {goal.description}"
        if extra_context:
            prompt += f"\n\n{extra_context}"
        if self._tool_instructions:
            prompt += self._tool_instructions
        now = datetime.now(UTC)
        try:
            local = now.astimezone(ZoneInfo(self._timezone)) if self._timezone else now.astimezone()
        except (KeyError, ValueError) as exc:
            logger.warning("Unknown timezone %r, falling back to local: %s", self._timezone, exc)
            local = now.astimezone()
        prompt += (
            f"\n\nCurrent date and time: "
            f"{local.strftime('%A, %Y-%m-%d %H:%M:%S %Z')}"
            f" (UTC: {now.strftime('%Y-%m-%d %H:%M:%S')})"
        )
        if reply_style:
            prompt += f"\n{reply_style}"
        if not allow_silence:
            prompt += "\nNever output <<silent>>. Always provide a substantive reply."
        return prompt

    def _get_history(self, conversation_id: str | None = None) -> list[ChatMessage]:
        key = self._history_key(conversation_id)
        if key in self._history:
            self._history.move_to_end(key)
        else:
            self._history[key] = []
            while len(self._history) > self._max_conversations:
                evicted_key, _ = self._history.popitem(last=False)
                logger.debug("Evicted conversation from history cache: %s", evicted_key)
        return self._history[key]

    def _build_messages(
        self,
        user_message: str,
        conversation_id: str | None = None,
        system_prompt: str | None = None,
        images: list[bytes] | None = None,
        extra_system_context: str | None = None,
        reply_style: str | None = None,
        allow_silence: bool = True,
    ) -> list[ChatMessage]:
        self._sync_goal_state()
        system = self._get_system_prompt(
            overrides=system_prompt,
            extra_context=extra_system_context,
            reply_style=reply_style,
            allow_silence=allow_silence,
        )
        messages = [ChatMessage(role=MessageRole.SYSTEM, content=system)]
        messages.extend(self._get_history(conversation_id))

        if images:
            blocks: list = [TextBlock(text=user_message)]
            for img_bytes in images:
                blocks.append(ImageBlock(image=img_bytes))
            messages.append(ChatMessage(role=MessageRole.USER, blocks=blocks))
        else:
            messages.append(ChatMessage(role=MessageRole.USER, content=user_message))
        return messages

    def _trim_history(self, conversation_id: str | None = None) -> None:
        key = self._history_key(conversation_id)
        history = self._history.get(key, [])
        if self._max_history == 0 or self._max_history_chars == 0:
            self._history[key] = []
            return

        trimmed: list[ChatMessage] = []
        total_chars = 0
        for message in reversed(history[-self._max_history :]):
            content_length = len(message.content or "")
            if trimmed and total_chars + content_length > self._max_history_chars:
                break
            trimmed.append(message)
            total_chars += content_length
        self._history[key] = list(reversed(trimmed))

    async def observe(
        self,
        message: str,
        conversation_id: str | None = None,
        context: MessageContext | None = None,
        images: list[bytes] | None = None,
    ) -> None:
        """Store a message in history without generating a reply."""
        if not message or not message.strip():
            if not images:
                return
        formatted = self._format_user_message(message, context)
        stored_text = self._history_placeholder(formatted, images)
        async with self._save_lock:
            self._get_history(conversation_id).append(
                ChatMessage(role=MessageRole.USER, content=stored_text)
            )
            self._trim_history(conversation_id)
        self._mark_dirty()

    def _history_placeholder(self, formatted: str, images: list[bytes] | None) -> str:
        if not images:
            return formatted
        size_kb = sum(len(b) for b in images) // 1024
        return f"{formatted}\n[image: {len(images)} image(s), {size_kb} KB]"

    async def chat(
        self,
        message: str,
        conversation_id: str | None = None,
        context: MessageContext | None = None,
        system_prompt: str | None = None,
        images: list[bytes] | None = None,
        store_user_message: bool = True,
        extra_system_context: str | None = None,
        reply_style: str | None = None,
        allow_silence: bool = True,
    ) -> str:
        formatted = self._format_user_message(message, context)
        messages = self._build_messages(
            formatted,
            conversation_id=conversation_id,
            system_prompt=system_prompt,
            images=images,
            extra_system_context=extra_system_context,
            reply_style=reply_style,
            allow_silence=allow_silence,
        )

        try:
            reply = await self._run_with_tools(messages) or "I couldn't generate a response."

            if is_silent_reply(reply):
                return "<<silent>>"

            async with self._save_lock:
                history = self._get_history(conversation_id)
                if store_user_message:
                    history.append(
                        ChatMessage(
                            role=MessageRole.USER,
                            content=self._history_placeholder(formatted, images),
                        )
                    )
                history.append(ChatMessage(role=MessageRole.ASSISTANT, content=reply))
                self._trim_history(conversation_id)
                self._sync_goal_state()
            self._mark_dirty()
            await asyncio.to_thread(self._save_goal)

            return reply
        except TimeoutError as exc:
            logger.warning("Agent chat timeout: %s", exc)
            return "Sorry, the language model took too long to respond."
        except Exception:
            logger.exception("Agent chat error")
            return "Sorry, I encountered an error processing your message."

    async def complete(self, prompt: str) -> str:
        """One-shot non-tool LLM completion, for lightweight checks.

        Used by features that need the model's judgment (e.g. goal-clarity
        checks) without the tool-calling loop or history side effects.
        Returns the raw message content (reasoning channels stripped), or
        an empty string on failure.
        """
        try:
            from llama_index.core.llms import ChatMessage, MessageRole

            resp = await self._llm.achat(
                chat_history=[ChatMessage(role=MessageRole.USER, content=prompt)]
            )
            content = resp.message.content or ""
            return strip_reasoning_channels(content) if isinstance(content, str) else str(content)
        except Exception:
            logger.warning("one-shot LLM completion failed", exc_info=True)
            return ""

    async def _run_with_tools(self, messages: list[ChatMessage]) -> str | None:
        scratchpad: list[ChatMessage] = []

        for _ in range(_MAX_TOOL_ROUNDS):
            response = await self._llm.achat_with_tools(
                tools=self._tools,
                chat_history=[*messages, *scratchpad],
            )

            tool_calls = self._llm.get_tool_calls_from_response(
                response, error_on_no_tool_call=False
            )

            scratchpad.append(response.message)

            if not tool_calls:
                content = response.message.content
                return strip_reasoning_channels(content) if isinstance(content, str) else content

            for tc in tool_calls:
                logger.info("Tool call: %s(%s)", tc.tool_name, tc.tool_kwargs)
                tool = self._tools_by_name.get(tc.tool_name)
                if tool is None:
                    result = f"Error: unknown tool '{tc.tool_name}'"
                else:
                    try:
                        output = await tool.acall(**tc.tool_kwargs)
                        result = str(output.content)
                    except Exception as exc:
                        logger.warning("Tool %s failed: %s", tc.tool_name, exc)
                        result = f"Error calling {tc.tool_name}: {exc}"

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

        try:
            final = await self._llm.achat(
                chat_history=[*messages, *scratchpad],
                tool_choice="none",
            )
            if final.message.content:
                return final.message.content
        except Exception:
            logger.warning("Final non-tool completion after tool-loop exhaustion failed")

        logger.warning(
            "Tool-loop exhaustion: %d rounds with no natural-language reply",
            _MAX_TOOL_ROUNDS,
        )
        return "I looked into it but couldn't finish gathering the information. Try again?"

    def _format_user_message(self, message: str, context: MessageContext | None = None) -> str:
        if context is None:
            return message
        suffix = " (mentioning you)" if context.is_group and context.mentions_bot else ""
        return f"[{context.sender_name}{suffix}] {message}"

    def _save_from_snapshot(self, snapshot: dict[str, list[ChatMessage]]) -> None:
        if self._history_file is None:
            return
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "history": {
                    key: [
                        {"role": message.role.value, "content": message.content or ""}
                        for message in messages
                    ]
                    for key, messages in snapshot.items()
                },
            }
            temp_path = Path(f"{self._history_file}.tmp")
            temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(self._history_file)
        except OSError as exc:
            logger.warning("Failed to save agent history to %s: %s", self._history_file, exc)

    def _mark_dirty(self) -> None:
        """Mark history as modified and schedule a debounced flush."""
        if self._history_file is None:
            return
        self._dirty = True
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.ensure_future(self._flush_after_delay())

    async def _flush_after_delay(self) -> None:
        try:
            await asyncio.sleep(_FLUSH_DELAY)
            await self._flush_now()
        except asyncio.CancelledError:
            await self._flush_now()

    async def _flush_now(self) -> None:
        async with self._save_lock:
            if not self._dirty:
                return
            snapshot = copy.deepcopy(self._history)
            self._dirty = False
        await asyncio.to_thread(self._save_from_snapshot, snapshot)

    async def flush(self) -> None:
        """Persist any pending history changes immediately."""
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        await self._flush_now()
