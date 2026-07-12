import asyncio
import copy
import json
import logging
import re
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
from llama_index.llms.openai import utils as _openai_utils
from llama_index.llms.openai_like import OpenAILike
from pydantic import BaseModel, ConfigDict, Field

from kai.agent.context import MessageContext
from kai.agent.goal import GoalManager
from kai.agent.tools import get_tool_instructions, get_tools
from kai.config.settings import Settings, get_settings

# Upper bound on tool-calling rounds per turn. Sized for the
# fact-checking workflow: one web_search round, then visiting several
# results one-per-round (some live-score/aggregator pages 403/406 and
# must be retried against the next result), plus a final synthesis
# round. The loop exits early as soon as the model stops calling tools.
_MAX_TOOL_ROUNDS = 24
_FLUSH_DELAY = 0.5

logger = logging.getLogger(__name__)

# --- VideoBlock shim --------------------------------------------------------
# llama_index's OpenAI adapter does not serialize VideoBlock — it raises
# ValueError("Unsupported content block type").  This monkeypatch makes
# to_openai_message_dict emit the verified ``video_url`` shape for VideoBlock,
# delegating all other block types to the original implementation.

_orig_to_openai_message_dict = _openai_utils.to_openai_message_dict


def _to_openai_message_dict(message, drop_none=False, model=None, store=False):
    if not any(isinstance(b, VideoBlock) for b in message.blocks):
        return _orig_to_openai_message_dict(message, drop_none=drop_none, model=model, store=store)
    content: list = []
    content_txt = ""
    for block in message.blocks:
        if isinstance(block, TextBlock):
            content.append({"type": "text", "text": block.text})
            content_txt += block.text
        elif isinstance(block, VideoBlock):
            if block.url:
                url = str(block.url)
            else:
                vid = block.resolve_video(as_base64=True).read()
                mt = block.video_mimetype or "video/mp4"
                url = f"data:{mt};base64,{vid.decode() if isinstance(vid, bytes) else vid}"
            content.append({"type": "video_url", "video_url": {"url": url}})
        else:
            single = _orig_to_openai_message_dict(
                type(message)(
                    role=message.role,
                    blocks=[block],
                    additional_kwargs=message.additional_kwargs,
                ),
                drop_none=drop_none,
                model=model,
                store=store,
            )
            item = single.get("content", "") if isinstance(single, dict) else single
            if isinstance(item, list):
                content.extend(item)
            elif isinstance(item, str):
                content.append({"type": "text", "text": item})
                content_txt += item
    return {
        "role": message.role.value,
        "content": (
            content_txt if all(isinstance(b, TextBlock) for b in message.blocks) else content
        ),
    }


_openai_utils.to_openai_message_dict = _to_openai_message_dict

# Type of an optional callback invoked when the agent calls a tool, so callers
# (e.g. a bot) can render tool usage live. Receives (tool_name, tool_kwargs,
# result); the result string may be empty for in-flight calls.
ToolCallCallback = Callable[[str, dict, str], None]

_DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
_VALID_ROLES = frozenset(r.value for r in MessageRole)


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

    action: str  # bot-defined vocabulary, e.g. "reply" | "silent" | "sleep"
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
    error: str | None = None  # None on success; set on timeout/exception/schema failure


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


_KV_LINE_RE = re.compile(r'^[\s\-*]*["\']?(\w+)["\']?\s*[:=]\s*(.*)$')


def _parse_kv_lines(text: str) -> dict[str, str]:
    """Parse ``key: value`` lines into a dict.

    Some models ignore the JSON instruction and emit the action as plain
    ``key: value`` lines (e.g. ``action: send_to_group\\ntext: hello``).
    This recovers those into a dict that ``model_validate`` can consume.
    """
    result: dict[str, str] = {}
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("```"):
            continue
        match = _KV_LINE_RE.match(line)
        if not match:
            continue
        key = match.group(1).strip()
        value = match.group(2).strip().rstrip(",").strip("\"'`")
        if key:
            result[key] = value
    return result


def _repair_json(text: str) -> str | None:
    """Attempt to extract and repair a JSON-like substring from text.

    Finds the first ``{``, then takes everything after it (to the end of
    the string or the last ``}``), and patches missing quotes and braces so
    partial JSON can still validate.
    """
    left = text.find("{")
    if left == -1:
        return None
    right = text.rfind("}")
    candidate = text[left : right + 1] if right > left else text[left:]
    quote_count = candidate.count('"')
    if quote_count % 2 == 1:
        candidate += '"'
    brace_count = candidate.count("{") - candidate.count("}")
    if brace_count > 0:
        candidate += "}" * brace_count
    return candidate


# Control (non-delivery) actions that base-class recovery must never rewrite
# into a deliverable turn. Recovering ``silent`` on a no-silent turn, for
# example, would ghost the user and bypass the bot's error-retry safety net.
# These names mirror the canonical waha vocabulary cited in ``ActionResult``'s
# docstring; a bot whose dispatch can degrade them safely is unaffected because
# recovery is opt-in (only fires for actions disallowed by this turn's schema).
_CONTROL_ACTIONS: frozenset[str] = frozenset({"silent", "sleep", "console"})


def _action_values(output_cls: type[ActionResult]) -> frozenset[str]:
    """Extract the allowed ``action`` enum values from a bot's ``output_cls``.

    The bot declares a ``Literal`` on its ``action`` field; pydantic exposes
    that as an ``enum`` in the JSON schema. Used to recognize when the model
    mistakenly emits an action value as a tool call.
    """
    try:
        schema = output_cls.model_json_schema()
        action_prop = schema.get("properties", {}).get("action", {})
        enum = action_prop.get("enum")
        if enum:
            return frozenset(enum)
    except Exception:
        pass
    return frozenset()


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
        self._tool_workflows: list[str] = []
        self._tool_instructions = get_tool_instructions(self._tools)
        self._tool_call_callback: ToolCallCallback | None = None
        # A single long-lived httpx.AsyncClient shared with the OpenAI SDK's
        # AsyncOpenAI client (via ``async_http_client=``). The SDK does not
        # take ownership/close it, so ``aclose()`` must be called on shutdown.
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        self._llm = self._build_llm(async_http_client=self._http)
        self._history: OrderedDict[str, list[ChatMessage]] = OrderedDict()
        self._timestamps: OrderedDict[str, list[str | None]] = OrderedDict()
        self._load_history_state()
        self._load_goal()
        self._dirty: bool = False
        self._flush_task: asyncio.Task | None = None
        self._timezone: str | None = None
        # Optional per-call temperature override (see set_temperature). When a
        # Brain is mandatory, bot.py lowers this (greedy decoding) to steer the
        # model toward following the MUST instruction to call brain_query. It
        # is steering, not a guarantee.
        self._temperature: float | None = None

    def _resolve_history_file(self) -> Path | None:
        folder = self.settings.agent_history_folder
        if folder is None:
            return None
        name = self._namespace or "default"
        return Path(folder) / f"{name}.json"

    @staticmethod
    def _now_ts() -> str:
        """ISO-8601 UTC timestamp for a history message."""
        return datetime.now(UTC).isoformat()

    def set_system_prompt(self, prompt: str, *, clear_history: bool = False) -> None:
        changed = prompt != self._system_prompt
        self._system_prompt = prompt
        logger.info("System prompt set (%d chars)", len(prompt))
        if changed and clear_history:
            self._history.clear()
            self._timestamps.clear()
            self._save_history()

    def register_tool(self, tool: FunctionTool) -> None:
        name = tool.metadata.name
        self._tools_by_name[name] = tool
        self._tools = [t for t in self._tools if t.metadata.name != name]
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
        """Add an optional tool-usage guidance block appended to the system prompt.

        Multiple calls **compose**: each non-``None`` workflow block is
        appended (deduplicated) to the existing set rather than replacing it,
        so e.g. a waha bot calling ``set_tool_workflow(WEB_WORKFLOW_INSTRUCTIONS)``
        in ``configure()`` and a later Brain-aware layer calling
        ``set_tool_workflow(BRAIN_WORKFLOW_INSTRUCTIONS)`` both survive in the
        prompt. Pass ``None`` to clear *all* workflow blocks back to the
        clean, tool-table-only prompt (e.g. a Docker ops bot that wants no
        workflow guidance at all). Use :meth:`clear_tool_workflows` if you
        need to drop workflows without also being unable to add ``None``.
        """
        if workflow is None:
            self._tool_workflows = []
        elif workflow not in self._tool_workflows:
            self._tool_workflows.append(workflow)
        self._tool_instructions = get_tool_instructions(
            self._tools, workflow_preamble=self._tool_workflow
        )

    def clear_tool_workflows(self) -> None:
        """Drop all previously added tool-workflow blocks."""
        self._tool_workflows = []
        self._tool_instructions = get_tool_instructions(
            self._tools, workflow_preamble=self._tool_workflow
        )

    @property
    def _tool_workflow(self) -> str | None:
        """The combined workflow preamble from all added blocks, or ``None``."""
        if not self._tool_workflows:
            return None
        return "\n\n".join(self._tool_workflows)

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

    def set_temperature(self, temperature: float | None) -> None:
        """Override the LLM temperature for all subsequent calls.

        When ``None`` (the default), the LLM uses its built-in default
        (0.1 for OpenAILike). Set to a lower value (e.g. 0.0) to make the
        model more deterministic. Used when a Brain is mandatory to steer the
        model toward following the MUST instruction to call brain_query first.
        This raises the probability of compliance; it does not guarantee it.
        Set back to ``None`` to restore the default.
        """
        self._temperature = temperature

    def set_tool_call_callback(self, callback: ToolCallCallback | None) -> None:
        """Register a callback fired whenever the agent calls a tool.

        The callback receives ``(tool_name, tool_kwargs, result)`` and is
        intended for live UI rendering (e.g. a bot printing tool usage). It is
        invoked after the tool returns, with the result string.
        """
        self._tool_call_callback = callback

    def _build_llm(
        self, async_http_client: httpx.AsyncClient | None = None
    ) -> OpenAILike:
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
            # Reuse the AsyncOpenAI client (and thus the shared httpx client)
            # across calls so connection pooling/keepalive is effective.
            reuse_client=True,
            async_http_client=async_http_client,  # type: ignore[call-arg]
            # ``PydanticProgramMode.LLM`` makes any ``as_structured_llm`` call
            # (used elsewhere in the framework) route through text completion +
            # ``PydanticOutputParser`` rather than function-calling programs.
            # The terminal structured step in ``_run_with_tools`` no longer
            # uses ``as_structured_llm`` — it calls ``achat`` directly with a
            # ``PydanticOutputParser``-formatted system message and a lenient
            # fallback parser (see ``_parse_structured_text``). This setting is
            # kept as a safety default for any other structured-predict caller.
            pydantic_program_mode=PydanticProgramMode.LLM,
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

    def _load_history_state(self) -> None:
        """Populate ``_history`` and ``_timestamps`` from the history file.

        Timestamps are optional: a missing or non-string ``ts`` field is
        recorded as ``None`` so the parallel-list invariant (one timestamp
        entry per message) is preserved across loads of old files.
        """
        self._history.clear()
        self._timestamps.clear()
        if self._history_file is None:
            return

        try:
            if not self._history_file.exists():
                return
            raw = json.loads(self._history_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                logger.warning("Ignoring invalid agent history file: %s", self._history_file)
                return

            history_data = raw.get("history", raw)
            if not isinstance(history_data, dict):
                return

            for key, messages in history_data.items():
                if not isinstance(key, str) or not isinstance(messages, list):
                    continue
                msgs: list[ChatMessage] = []
                ts_list: list[str | None] = []
                for item in messages:
                    if not isinstance(item, dict):
                        continue
                    role = item.get("role")
                    content = item.get("content")
                    if role in _VALID_ROLES and isinstance(content, str):
                        msgs.append(ChatMessage(role=MessageRole(role), content=content))
                        ts = item.get("ts")
                        ts_list.append(ts if isinstance(ts, str) else None)
                self._history[key] = msgs
                self._timestamps[key] = ts_list
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Failed to load agent history from %s: %s", self._history_file, exc)
            self._history.clear()
            self._timestamps.clear()

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
        self._save_from_snapshot(self._history, self._timestamps)

    @staticmethod
    def _serialize_history_message(message: ChatMessage, ts: str | None) -> dict:
        """Serialize one history message, including its optional timestamp."""
        item = {"role": message.role.value, "content": message.content or ""}
        if ts:
            item["ts"] = ts
        return item

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

    def _get_system_prompt(
        self,
        overrides: str | None = None,
        extra_context: str | None = None,
        reply_style: str | None = None,
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
        return prompt

    def _get_history(self, conversation_id: str | None = None) -> list[ChatMessage]:
        key = self._history_key(conversation_id)
        if key in self._history:
            self._history.move_to_end(key)
            # Self-heal a parallel-list divergence (e.g. a test or external
            # caller mutating _history directly): ensure _timestamps has a
            # matching entry, backfilling with None when it doesn't.
            if key not in self._timestamps:
                self._timestamps[key] = [None] * len(self._history[key])
            else:
                self._timestamps.move_to_end(key)
        else:
            self._history[key] = []
            self._timestamps[key] = []
            while len(self._history) > self._max_conversations:
                evicted_key, _ = self._history.popitem(last=False)
                self._timestamps.pop(evicted_key, None)
                logger.debug("Evicted conversation from history cache: %s", evicted_key)
        return self._history[key]

    def _build_messages(
        self,
        user_message: str,
        conversation_id: str | None = None,
        system_prompt: str | None = None,
        images: list[bytes] | None = None,
        videos: list[bytes] | None = None,
        extra_system_context: str | None = None,
        reply_style: str | None = None,
    ) -> list[ChatMessage]:
        system = self._get_system_prompt(
            overrides=system_prompt,
            extra_context=extra_system_context,
            reply_style=reply_style,
        )
        messages = [ChatMessage(role=MessageRole.SYSTEM, content=system)]
        messages.extend(self._get_history(conversation_id))

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

    def _trim_history(self, conversation_id: str | None = None) -> None:
        key = self._history_key(conversation_id)
        history = self._history.get(key, [])
        ts_list = self._timestamps.get(key, [])
        if self._max_history == 0 or self._max_history_chars == 0:
            self._history[key] = []
            self._timestamps[key] = []
            return

        # Iterate the tail (capped to _max_history) newest-first, dropping the
        # oldest entries that breach the char budget. Track the surviving
        # indices so the parallel timestamps list is trimmed to match — a
        # divergence (e.g. direct _history mutation in tests) is tolerated:
        # an out-of-range index yields None.
        tail = history[-self._max_history :] if self._max_history > 0 else history
        base = len(history) - len(tail)
        trimmed: list[ChatMessage] = []
        kept_idx: list[int] = []
        total_chars = 0
        for j, message in enumerate(reversed(tail)):
            content_length = len(message.content or "")
            if trimmed and total_chars + content_length > self._max_history_chars:
                break
            trimmed.append(message)
            kept_idx.append(base + len(tail) - 1 - j)
            total_chars += content_length
        kept_idx.reverse()
        self._history[key] = list(reversed(trimmed))
        self._timestamps[key] = [(ts_list[i] if i < len(ts_list) else None) for i in kept_idx]

    async def clear_history(self, conversation_id: str | None = None) -> None:
        """Clear a single conversation bucket's history (e.g. a ``/clear`` route).

        Mutates ``_history`` under ``_save_lock`` — like every other mutator
        (``observe``, ``record_assistant_message``, ``chat``) — so it can't
        race with a concurrent ``_flush_now`` snapshot.
        """
        key = self._history_key(conversation_id)
        async with self._save_lock:
            self._history[key] = []
            self._timestamps[key] = []
        self._mark_dirty()

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
        formatted = self._format_user_message(message, context)
        stored_text = self._history_placeholder(formatted, images, videos)
        async with self._save_lock:
            self._get_history(conversation_id).append(
                ChatMessage(role=MessageRole.USER, content=stored_text)
            )
            self._timestamps.setdefault(self._history_key(conversation_id), []).append(
                self._now_ts()
            )
            self._trim_history(conversation_id)
        self._mark_dirty()

    def _history_placeholder(
        self,
        formatted: str,
        images: list[bytes] | None,
        videos: list[bytes] | None = None,
    ) -> str:
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
        so the target chat sees "Kai: <what it sent>" and retains continuity,
        without a synthetic user turn polluting it.
        """
        if not text or not text.strip():
            return
        key = self._history_key(conversation_id)
        async with self._save_lock:
            self._get_history(conversation_id).append(
                ChatMessage(role=MessageRole.ASSISTANT, content=text)
            )
            self._timestamps.setdefault(key, []).append(self._now_ts())
            self._trim_history(conversation_id)
        self._mark_dirty()

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
        formatted = self._format_user_message(message, context)
        messages = self._build_messages(
            formatted,
            conversation_id=conversation_id,
            system_prompt=system_prompt,
            images=images,
            videos=videos,
            extra_system_context=extra_system_context,
            reply_style=reply_style,
        )

        try:
            action, tool_calls = await self._run_with_tools(
                messages, output_cls=output_cls, tools=tools
            )

            reply_text = action.text or ""

            # An action whose ``text`` is not actually a reply to *this*
            # conversation — e.g. an operator turn's ``send_to_group``/
            # ``send_dm``, where ``text`` is the message for a different
            # target chat — must not be recorded as an assistant reply here.
            # The caller (bot) is responsible for recording that text in the
            # *target* conversation's history once delivery is confirmed
            # (see ``record_assistant_message``). Recording it here too would
            # duplicate it, and would falsely show it as delivered to *this*
            # conversation even if the actual send later fails.
            delegated = is_delegated_action is not None and is_delegated_action(action)

            # The inbound user message is recorded independently of the
            # assistant reply when the turn produced *something* — either a
            # reply for this conversation, or a delegated send (whose text
            # goes to a different chat but whose inbound instruction must
            # still be visible in this conversation's history). A bare
            # ``silent`` turn (empty reply, not delegated) stores nothing
            # here: the bot's own ``_abort_turn``/``observe()`` path records
            # the inbound separately, so storing it here too would duplicate.
            save_assistant = bool(reply_text) and not delegated
            save_user = store_user_message and (bool(reply_text) or delegated)
            if save_user or save_assistant:
                async with self._save_lock:
                    history = self._get_history(conversation_id)
                    key = self._history_key(conversation_id)
                    ts_list = self._timestamps.setdefault(key, [])
                    if save_user:
                        history.append(
                            ChatMessage(
                                role=MessageRole.USER,
                                content=self._history_placeholder(formatted, images, videos),
                            )
                        )
                        ts_list.append(self._now_ts())
                    if save_assistant:
                        history.append(ChatMessage(role=MessageRole.ASSISTANT, content=reply_text))
                        ts_list.append(self._now_ts())
                    self._trim_history(conversation_id)
                self._mark_dirty()
            await asyncio.to_thread(self._save_goal)

            return ChatResult(reply=reply_text, tool_calls=tool_calls, action=action)
        except TimeoutError as exc:
            logger.warning("Agent chat timeout: %s", exc)
            return ChatResult(
                reply="",
                tool_calls=[],
                action=ActionResult(
                    action="error",
                    text="Sorry, the language model took too long to respond.",
                ),
                error=f"timeout: {exc}",
            )
        except Exception as exc:
            logger.exception("Agent chat error")
            return ChatResult(
                reply="",
                tool_calls=[],
                action=ActionResult(
                    action="error",
                    text="Sorry, I encountered an error processing your message.",
                ),
                error=str(exc),
            )

    async def complete(self, prompt: str) -> str:
        """One-shot non-tool LLM completion, for lightweight checks.

        Used by features that need the model's judgment (e.g. goal-clarity
        checks) without the tool-calling loop or history side effects.
        Returns the raw message content (reasoning channels stripped), or
        an empty string on failure.
        """
        try:
            resp = await self._llm.achat(
                messages=[ChatMessage(role=MessageRole.USER, content=prompt)]
            )
            content = resp.message.content or ""
            return strip_reasoning_channels(content) if isinstance(content, str) else str(content)
        except Exception:
            logger.warning("one-shot LLM completion failed", exc_info=True)
            return ""

    async def _run_with_tools(
        self,
        messages: list[ChatMessage],
        output_cls: type[ActionResult],
        tools: list[FunctionTool] | None = None,
    ) -> tuple[ActionResult, list[ToolCallRecord]]:
        """Run the tool-calling loop, then resolve a typed ``output_cls`` action.

        Returns ``(action, tool_calls)`` where ``action`` is an instance of
        the bot-declared ``output_cls`` and ``tool_calls`` records every
        side-effecting tool invocation. The terminal step is always a
        schema-constrained ``astructured_predict`` call — there is no
        free-form ``achat`` path left for the model to leak markup through.
        """
        active_tools = tools if tools is not None else self._tools
        # When a per-turn allowlist is given, dispatch looks tools up within
        # that subset; otherwise it falls back to the globally-registered
        # ``_tools_by_name`` dict (which callers may override per-test).
        active_by_name = {t.metadata.name: t for t in active_tools} if tools is not None else None
        scratchpad: list[ChatMessage] = []
        tool_call_records: list[ToolCallRecord] = []
        # The closed set of action values the bot's ``output_cls`` allows. When
        # a model emits one of these as a (nonexistent) tool call — the
        # "action values are NOT tools" confusion — we treat it as the turn's
        # terminal decision instead of recording a failed dispatch.
        action_values = _action_values(output_cls)
        # Build per-call LLM kwargs. Temperature override makes the model
        # more deterministic when e.g. Brain is mandatory — greedy decoding
        # increases the chance the model follows MUST instructions.
        llm_kwargs: dict = {}
        if self._temperature is not None:
            llm_kwargs["temperature"] = self._temperature

        for _ in range(_MAX_TOOL_ROUNDS):
            response = await self._llm.achat_with_tools(
                tools=active_tools,
                chat_history=[*messages, *scratchpad],
                **llm_kwargs,
            )

            tool_calls = self._llm.get_tool_calls_from_response(
                response, error_on_no_tool_call=False
            )

            scratchpad.append(response.message)

            if not tool_calls:
                break

            for tc in tool_calls:
                logger.info("Tool call: %s(%s)", tc.tool_name, tc.tool_kwargs)
                if tc.tool_name in action_values and tc.tool_name not in self._tools_by_name:
                    # The model expressed its decision as a tool call on a
                    # name that is actually an action value. Capture it as
                    # the typed action and end the turn here — no spurious
                    # "unknown tool" failure, no extra LLM round.
                    action = self._action_from_tool_call(output_cls, tc.tool_name, tc.tool_kwargs)
                    if action is not None:
                        logger.info("Resolved action-as-tool: %s(%s)", tc.tool_name, tc.tool_kwargs)
                        return action, tool_call_records
                    # If the kwargs didn't validate, fall through to dispatch
                    # (which will record the unknown-tool failure) and let the
                    # terminal structured step retry.
                if active_by_name is not None:
                    tool = active_by_name.get(tc.tool_name)
                    if tool is None:
                        # Allowlist miss: fall back to globally registered tools
                        # rather than silently no-oping.
                        tool = self._tools_by_name.get(tc.tool_name)
                else:
                    tool = self._tools_by_name.get(tc.tool_name)
                if tool is None:
                    result = f"Error: unknown tool '{tc.tool_name}'"
                    ok = False
                else:
                    try:
                        output = await tool.acall(**tc.tool_kwargs)
                        result = str(output.content)
                        ok = True
                    except Exception as exc:
                        logger.warning("Tool %s failed: %s", tc.tool_name, exc)
                        result = f"Error calling {tc.tool_name}: {exc}"
                        ok = False

                tool_call_records.append(
                    ToolCallRecord(
                        name=tc.tool_name, args=dict(tc.tool_kwargs), ok=ok, result=result
                    )
                )
                logger.info(
                    "Tool result: %s -> %s (%d chars)",
                    tc.tool_name,
                    "ok" if ok else "error",
                    len(result),
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

        # Terminal step: resolve the turn's action as a typed ``output_cls``.
        #
        # We call the LLM directly (not through ``as_structured_llm``) so we
        # control parsing and can retry. The JSON-schema instruction is
        # appended to the leading system message. We build it ourselves
        # rather than via ``PydanticOutputParser.format_messages``: that
        # helper emits the schema with doubled braces (``{{ ... }}``) because
        # the string is meant for a ``PromptTemplate``, and its wording
        # ("Output a valid JSON object but do not repeat the schema.") is too
        # weak — models often answer simple questions in plain prose, which
        # fails the parse and triggers a wasted retry LLM call. A stronger,
        # valid-JSON instruction in the system message keeps the user's
        # directive as the most recent turn (so directive-following is
        # preserved) while still pushing the model toward JSON.
        #
        # The reply is parsed leniently:
        #   1. Standard ``extract_json_str`` (regex ``{.*}`` — handles JSON
        #      embedded in prose or markdown fences).
        #   2. ``key: value`` line parser for models that ignore the JSON
        #      instruction and emit ``action: send_to_group\ntext: ...``.
        #   3. JSON repair (patch missing quotes/braces).
        # If all three fail, the step is retried once with an explicit
        # correction message ("return ONLY JSON") before giving up.
        from llama_index.core.output_parsers.pydantic import PydanticOutputParser

        parser = PydanticOutputParser(output_cls=output_cls)
        schema = json.dumps(output_cls.model_json_schema(), ensure_ascii=False)
        json_instruction = (
            "\n\n## Output format (mandatory)\n"
            "Respond with ONLY a single JSON object matching this schema — "
            "no prose, no markdown, no code fences, no `key: value` lines. "
            "Choose the action and fill its fields; the whole response must "
            "be that one JSON object.\n\n"
            f"Schema: {schema}"
        )
        sys_msg = messages[0]
        if sys_msg.role == MessageRole.SYSTEM:
            augmented = ChatMessage(
                role=MessageRole.SYSTEM, content=(sys_msg.content or "") + json_instruction
            )
            predict_messages = [augmented, *messages[1:], *scratchpad]
        else:
            predict_messages = [
                ChatMessage(role=MessageRole.SYSTEM, content=json_instruction),
                *messages,
                *scratchpad,
            ]

        action: ActionResult | None = None
        for attempt in range(2):
            try:
                response = await self._llm.achat(messages=predict_messages, **llm_kwargs)
            except Exception as exc:
                logger.warning("Structured prediction LLM call failed: %s", exc)
                raise

            raw_text = strip_reasoning_channels(response.message.content or "")
            try:
                action = self._parse_structured_text(raw_text, output_cls, parser)
                break
            except Exception as exc:
                if attempt == 0:
                    logger.warning(
                        "Structured output parse failed, retrying with correction: %s",
                        exc,
                    )
                    predict_messages = [
                        *predict_messages,
                        ChatMessage(role=MessageRole.ASSISTANT, content=raw_text),
                        ChatMessage(
                            role=MessageRole.USER,
                            content=(
                                "That was not valid JSON. Return ONLY a JSON "
                                "object matching the schema — no prose, no "
                                "code fences, no field labels."
                            ),
                        ),
                    ]
                else:
                    logger.warning("Structured output parse failed after retry: %s", exc)
                    raise

        if action is None:
            # Unreachable in practice: the loop above either sets ``action``
            # via a successful parse or raises. Guard against -O (which strips
            # asserts) so a logic drift surfaces as a clear error, not a None
            # returned where an ActionResult is required.
            raise RuntimeError("structured output parsing returned None unexpectedly")
        if action.text:
            action = action.model_copy(update={"text": strip_reasoning_channels(action.text)})
        return action, tool_call_records

    @staticmethod
    def _parse_structured_text(
        text: str,
        output_cls: type[ActionResult],
        parser: Any,
    ) -> ActionResult:
        """Parse a model reply into ``output_cls``, trying several strategies.

        1. Standard ``PydanticOutputParser.parse`` — extracts JSON via regex
           ``{.*}``, which handles JSON embedded in prose or markdown fences.
        2. ``key: value`` line parser — for models that emit
           ``action: send_to_group\\ntext: hello`` instead of JSON.
        3. JSON repair — patches missing quotes/braces on partial JSON.
        4. Base-class recovery — if the payload is well-formed but the bot's
           constrained ``action`` Literal rejects the value the model chose
           (e.g. the user insisted on a voice note while TTS is offline, so
           ``send_voice_note`` is not in this turn's schema), validate against
           the unconstrained ``ActionResult`` base and let the bot's dispatch
           degrade gracefully. The waha bot's ``send_voice_note`` path already
           falls back to a text delivery when voice synthesis is unavailable,
           so this turns a hard crash into a delivered message.
        """
        # 1. Standard JSON extraction
        try:
            result = parser.parse(text)
            if isinstance(result, output_cls):
                return result
            return output_cls.model_validate(result.model_dump())
        except Exception:
            pass

        # 2. key: value line format
        kv = _parse_kv_lines(text)
        if kv:
            try:
                return output_cls.model_validate(kv)
            except Exception:
                pass

        # 3. JSON repair
        repaired = _repair_json(text)
        if repaired:
            try:
                return output_cls.model_validate_json(repaired)
            except Exception:
                pass

        # 4. Base-class recovery for a well-formed payload whose ``action``
        #    value is outside this turn's constrained vocabulary. Scoped to
        #    *delivery* actions — ones carrying a message the bot can still
        #    deliver (possibly via a fallback, e.g. waha's send_voice_note
        #    falls back to text when TTS is offline). Control actions
        #    (silent / sleep / console) are deliberately excluded: recovering
        #    ``silent`` on a no-silent turn would ghost the user and bypass
        #    the bot's error-retry safety net, which is worse than the
        #    original crash. A payload that fails for any other reason
        #    (missing/ill-typed fields, or an allowed action that still
        #    fails validation) continues to raise so genuinely malformed
        #    output is not silently accepted.
        control_actions = frozenset({"silent", "sleep", "console"})
        allowed = _action_values(output_cls)
        payload: dict[str, Any] | None = None
        if repaired:
            try:
                payload = json.loads(repaired)
            except Exception:
                payload = None
        if payload is None and kv:
            payload = kv
        if (
            isinstance(payload, dict)
            and "action" in payload
            and allowed
            and str(payload["action"]) not in allowed
        ):
            try:
                base = ActionResult.model_validate(payload)
            except Exception:
                base = None
            if base is not None and base.action not in control_actions and bool(base.text):
                logger.warning(
                    "Structured output: action %r is not in the %s "
                    "vocabulary for this turn; recovering via base "
                    "ActionResult so dispatch can degrade gracefully "
                    "(e.g. voice->text fallback).",
                    base.action,
                    output_cls.__name__,
                )
                return base

        raise ValueError(f"Could not parse structured output from: {text[:200]!r}")

    @staticmethod
    def _action_from_tool_call(
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
            if action.text:
                action = action.model_copy(update={"text": strip_reasoning_channels(action.text)})
            return action
        except Exception:
            return None

    def _format_user_message(self, message: str, context: MessageContext | None = None) -> str:
        if context is None:
            return message
        suffix = " (addressing you)" if context.multi_party and context.addressed_to_bot else ""
        header = f"[{context.sender_name}{suffix}]"
        # When the message is a multi-line enrichment block (reply-to / links /
        # voice / image tags stacked above the body), keep the speaker header on
        # its own line so it doesn't fuse with the first metadata tag.
        if "\n" in message:
            return f"{header}\n{message}"
        return f"{header} {message}"

    def _save_from_snapshot(
        self,
        snapshot: dict[str, list[ChatMessage]],
        timestamps: dict[str, list[str | None]] | None = None,
    ) -> None:
        if self._history_file is None:
            return
        if timestamps is None:
            timestamps = self._timestamps
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            serialized: dict[str, list[dict]] = {}
            for key, messages in snapshot.items():
                ts_list = timestamps.get(key, [])
                items = []
                for i, message in enumerate(messages):
                    ts = ts_list[i] if i < len(ts_list) else None
                    items.append(self._serialize_history_message(message, ts))
                serialized[key] = items
            data = {"history": serialized}
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
            ts_snapshot = {k: list(v) for k, v in self._timestamps.items()}
            self._dirty = False
        await asyncio.to_thread(self._save_from_snapshot, snapshot, ts_snapshot)

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

    async def aclose(self) -> None:
        """Release the shared httpx client backing the LLM.

        After this the agent's LLM must not be used again. Call during
        shutdown, after ``flush()``.
        """
        if not self._http.is_closed:
            await self._http.aclose()
