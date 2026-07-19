"""Chat-history tool and helpers for the WAHA bot.

Extracted from ``__init__.py`` to keep the main bot module lean.  The
:func:`register_chat_history_tool` function is called once during
:meth:`Bot.configure` to register the ``get_whatsapp_history`` LLM tool.
"""

import logging
from typing import TYPE_CHECKING

from llama_index.core.tools import FunctionTool

from kai.agent.core import KaiAgent
from kai.bots.waha.client import WahaClient
from kai.bots.waha.jid import sanitize_display_name
from kai.bots.waha.mentions import resolve_inbound_mentions
from kai.bots.waha.payload import GROUP_SUFFIX, _extract_sender_id, _extract_sender_name

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from kai.bots.waha import Bot


def register_chat_history_tool(
    agent: KaiAgent,
    *,
    bot: "Bot",
) -> None:
    """Register ``get_whatsapp_history``, scoped to the current chat.

    The tool fetches past messages from WAHA's chat-history endpoint so the
    model can summarize or recap a conversation it wasn't online for. It
    reads ``chat_id`` from :class:`ToolContext` at call time (set per-turn by
    :meth:`BaseBot.set_task_context`), so concurrent chats never
    cross-contaminate. Results live in the agent's scratchpad for the
    current turn only — they are NOT injected into the trimmed LRU history,
    so there's no history bloat and no per-message token cost on normal turns.

    Parameters
    ----------
    agent:
        The :class:`KaiAgent` to register the tool on.
    bot:
        The :class:`Bot` instance (used to access ``_tool_context``,
        ``_waha_client``, ``_waha``, and ``_rosters`` at call time).
    """

    async def get_whatsapp_history(limit: int = 50, offset: int = 0) -> str:
        """Fetch past messages from this chat's history.

        Use when asked to summarize or recap a conversation, including
        messages sent before the bot was online. Returns messages
        formatted as '[SenderName] body', oldest first, one per line.

        Args:
            limit: Number of messages to fetch (default 50, max 200).
            offset: Skip this many recent messages to page into older
                history. 0 = most recent batch; 50 = the next older 50.
        """
        tool_context = bot._tool_context
        if tool_context is None:
            return "Error: no chat context available"
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        chat_id = tool_context.current().chat_id
        if not chat_id:
            return "Error: no chat context available"

        client: WahaClient | None = bot._waha_client
        should_close = False
        if client is None:
            from kai.bots.waha.client import WahaClient as _WahaClient

            client = _WahaClient(bot._waha)
            should_close = True
        try:
            messages = await client.get_chat_messages(chat_id, limit=limit, offset=offset)
        except Exception as exc:
            logger.warning("get_whatsapp_history fetch failed: %s", exc)
            return f"Error: could not fetch chat history ({exc})"
        finally:
            if should_close:
                await client.close()

        # WAHA returns newest-first; reverse so the model reads chronologically.
        roster: dict[str, str] = bot._rosters.get(chat_id, {})
        is_group = GROUP_SUFFIX in chat_id
        lines: list[str] = []
        for m in reversed(messages):
            body = (m.get("body") or "").strip()
            if not body:
                continue
            if bool(m.get("fromMe")):
                name = "kAI"
            else:
                sender_id = _extract_sender_id(m)
                name = roster.get(sender_id)
                if name:
                    name = sanitize_display_name(name)
                else:
                    name = _extract_sender_name(m, sender_id)
            # Resolve inbound @<digits> mentions so the recap shows names, not
            # raw LID/phone digits (mirrors live message enrichment).
            body = resolve_inbound_mentions(body, roster, is_group=is_group)
            lines.append(f"[{name}] {body}")
        return "\n".join(lines) if lines else "No text messages found."

    agent.register_tool(
        FunctionTool.from_defaults(
            fn=get_whatsapp_history,
            name="get_whatsapp_history",
            description=(
                "Fetch past messages from this chat's WhatsApp history "
                "(including from before the bot was online). Returns "
                "messages formatted as '[SenderName] body', oldest first. "
                "Use when asked to summarize or recap the conversation. "
                "Pass limit (default 50, max 200) and offset (0 = most "
                "recent) to page through older history. Call multiple "
                "times with increasing offset for a long recap."
            ),
        )
    )
