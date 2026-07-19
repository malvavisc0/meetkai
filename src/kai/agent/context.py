from contextvars import ContextVar

from pydantic import BaseModel, ConfigDict


class MessageContext(BaseModel, frozen=True):
    """Transport-agnostic descriptor of an inbound message.

    The agent runtime never knows what a "mention", a "group", or a "JID"
    is. A bot parses its transport-specific event into these generic fields
    and hands the result to :meth:`KaiAgent.chat`. Transport-specific
    metadata (reply-to bodies, voice transcripts, container exit codes,
    email subjects) stays in the bot's parsed object and is injected into
    the message text as tags the bot already produces.
    """

    sender_name: str
    sender_id: str = ""
    conversation_id: str = ""  # conversation identifier
    multi_party: bool = False  # multi-participant conversation
    addressed_to_bot: bool = False  # whether the message directly addresses the bot


class ChatContext(BaseModel):
    """Immutable snapshot of the chat a tool call should operate on."""

    model_config = ConfigDict(frozen=True)

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
