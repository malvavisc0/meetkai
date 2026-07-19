"""Conversation tools — read and write notes in the agent's internal history.

These tools operate on :class:`KaiAgent`'s ``_history`` store.

- ``get_conversation_messages`` — read messages for a specific conversation.
- ``record_note`` — write a note into a conversation's history bucket.
"""

import logging

from llama_index.core.llms import ChatMessage, MessageRole
from llama_index.core.tools import FunctionTool

from kai.agent.context import ToolContext
from kai.agent.core import KaiAgent

logger = logging.getLogger(__name__)

_NOTE_PREFIX = "[note]"

_ROLE_LABELS = {
    MessageRole.USER: "User",
    MessageRole.ASSISTANT: "Assistant",
}


def _format_history(messages: list[ChatMessage]) -> str:
    """Render a conversation's messages as ``[Role] body`` lines."""
    lines: list[str] = []
    for msg in messages:
        content = (msg.content or "").strip()
        if not content:
            continue
        label = _ROLE_LABELS.get(msg.role, msg.role.value)
        lines.append(f"[{label}] {content}")
    return "\n".join(lines) if lines else "No messages found."


def _format_all_conversations(agent: KaiAgent) -> str:
    """Render every known conversation, most recently active first."""
    conversations = agent.list_conversations()
    if not conversations:
        return "No conversations found."
    sections = [
        f"=== {cid} ===\n{_format_history(agent.get_conversation_history(cid))}"
        for cid, _count, _last_ts in conversations
    ]
    return "\n\n".join(sections)


def register_conversation_tools(
    agent: KaiAgent,
    *,
    tool_context: ToolContext | None,
) -> None:
    """Register ``get_conversation_messages`` and ``record_note`` on ``agent``.

    When ``tool_context`` is provided, an empty ``conversation_id`` falls
    back to the current chat; when ``None``, empty id returns all known
    conversations.
    """

    def _resolve(conversation_id: str) -> str:
        if conversation_id.strip():
            return conversation_id.strip()
        if tool_context is not None:
            chat_id = tool_context.current().chat_id
            if chat_id:
                return chat_id
        return ""

    async def get_conversation_messages(conversation_id: str = "") -> str:
        """Read the stored messages for a conversation, or list all of them.

        Returns the messages the bot has seen and sent in that conversation
        (including any notes), formatted as '[Role] body', oldest first. Use
        to summarize or recall a conversation — including from the operator
        console when the conversation_id is a different one than the current
        turn. Leave conversation_id empty to see every known conversation
        (grouped by conversation_id) when you don't have — or aren't sure
        of — the exact address/JID.

        Args:
            conversation_id: The conversation to read (email address, WhatsApp
                JID, etc.). Leave empty to read the current conversation, or
                every conversation if there is no current one (e.g. an
                operator turn).
        """
        resolved = _resolve(conversation_id)
        if not resolved:
            return _format_all_conversations(agent)
        messages = agent.get_conversation_history(resolved)
        return _format_history(messages)

    async def record_note(note: str, conversation_id: str = "") -> str:
        """Record a note into a conversation's history without sending a message.

        The note persists and appears on that conversation's future turns, so
        it reaches the reply decision. Use for facts worth remembering about a
        specific conversation (e.g. a customer's tier, a preference) — NOT for
        global behavioral directives (use the goal/settings for those).

        Args:
            note: The note text to store.
            conversation_id: The conversation to store the note in. Leave empty
                to target the current conversation.
        """
        note = (note or "").strip()
        if not note:
            return "Error: note text is required"
        resolved = _resolve(conversation_id)
        if not resolved:
            return "Error: no conversation_id provided and no current chat context available."
        await agent.record_assistant_message(resolved, f"{_NOTE_PREFIX} {note}")
        return f"note recorded for {resolved}: {note[:100]}"

    agent.register_tool(
        FunctionTool.from_defaults(
            fn=get_conversation_messages,
            name="get_conversation_messages",
            description=(
                "Read the stored messages for a conversation (the bot's own "
                "memory, not the transport's message log). Returns messages "
                "formatted as '[Role] body', oldest first. Pass "
                "conversation_id (an email address or WhatsApp JID) to read a "
                "specific conversation; leave empty for the current one, or "
                "to list every known conversation if there is no current one "
                "(e.g. an operator turn with no specific address/JID yet). "
                "Use to summarize or recall a conversation, including from "
                "the operator console."
            ),
        )
    )
    agent.register_tool(
        FunctionTool.from_defaults(
            fn=record_note,
            name="record_note",
            description=(
                "Record a note into a conversation's history without sending "
                "any message. The note persists and appears on that "
                "conversation's future turns. Use for facts worth remembering "
                "about a specific conversation (customer tier, preference). "
                "Do NOT use for global behavioral directives — use the goal "
                "or settings for those."
            ),
        )
    )
