from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class MessageContext:
    sender_name: str
    sender_id: str = ""
    chat_id: str = ""
    is_group: bool = False
    mentions_bot: bool = False


@dataclass(frozen=True)
class ChatContext:
    """Immutable snapshot of the chat a tool call should operate on."""

    chat_id: str = ""
    owner_id: str = ""
    tz_hint: str | None = None


class ToolContext:
    """Provides the per-call chat context (chat_id, owner, tz hint) to tools.

    The current context is stored in a :class:`contextvars.ContextVar` rather
    than as a shared mutable field. This is critical for correctness: a bot may
    handle messages from several chats concurrently (e.g. one asyncio task per
    inbound webhook), and each ``agent.chat()`` call runs in its own context.
    A ``ContextVar`` gives every task an isolated value, so a reminder created
    in one chat can never be misrouted to another chat that happened to be in
    flight at the same time.

    Bots call :meth:`set` (via ``BaseBot.set_task_context``) at the start of
    handling an inbound message; tools read :meth:`current` during that same
    task.
    """

    def __init__(self, chat_id: str = "", owner_id: str = "", tz_hint: str | None = None) -> None:
        self._var: ContextVar[ChatContext] = ContextVar("kai_chat_context")
        self._default = ChatContext(chat_id=chat_id, owner_id=owner_id, tz_hint=tz_hint)

    def set(self, chat_id: str, owner_id: str = "", tz_hint: str | None = None) -> None:
        self._var.set(ChatContext(chat_id=chat_id, owner_id=owner_id, tz_hint=tz_hint))

    def current(self) -> ChatContext:
        return self._var.get(self._default)
