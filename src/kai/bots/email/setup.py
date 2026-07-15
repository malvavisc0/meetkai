"""Bot-level configuration model for the email support bot.

Distinct from ``kai.bots.email.config`` which holds the transport settings
(``KAI_BOT_*`` env). This is the operator-overridable config (language,
timezone) — a minimal subset of the waha bot's ``BotConfig`` (no media, no
participation, no trigger_keyword/whitelist). ``blacklist`` is the one
list-type setting the email bot does support: unlike waha's chat
whitelist/blacklist, there's no "allow only these senders" concept here —
only a blocklist of sender addresses to silently ignore.
"""

from pydantic import BaseModel, ConfigDict, Field

from kai.agent.tools.email import DEFAULT_DISPLAY_NAME


class BotConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    language: str = "English"
    timezone: str | None = "Europe/Berlin"
    # The sender identity shown in the "From" header of outbound replies
    # (see agent/tools/email.py::format_from_header). Per-deployment rather
    # than on the shared SMTP connection, since one operator's SMTP
    # credential can back multiple deployments with different personas.
    display_name: str = DEFAULT_DISPLAY_NAME
    # Sender email addresses to silently ignore in ingest_event, before any
    # attachment download or agent turn. Checked fresh from this list on
    # every inbound email — no block history is persisted.
    blacklist: list[str] = Field(default_factory=list)
    # LLM sampling temperature (passed to agent.set_temperature() in
    # configure()). Left un-set, the provider's own default applies, which
    # varies by backend and is often not low. A support bot answering from a
    # Brain must follow its instructions reliably — ground in brain_query,
    # never invent facts, stay concise — so this defaults lower than the
    # waha persona bot's 0.4.
    temperature: float = 0.2
