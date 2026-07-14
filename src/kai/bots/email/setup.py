"""Bot-level configuration model for the email support bot.

Distinct from ``kai.bots.email.config`` which holds the transport settings
(``KAI_BOT_*`` env). This is the packaged/overridable config (language,
timezone) — a minimal subset of the waha bot's ``BotConfig`` (no media, no
participation, no trigger_keyword/whitelist/blacklist).
"""

from pydantic import BaseModel, ConfigDict


class BotConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    language: str = "English"
    timezone: str | None = None
    # LLM sampling temperature (passed to agent.set_temperature() in
    # configure()). Left un-set, the provider's own default applies, which
    # varies by backend and is often not low. A support bot answering from a
    # Brain must follow its instructions reliably — ground in brain_query,
    # never invent facts, stay concise — so this defaults lower than the
    # waha persona bot's 0.4.
    temperature: float = 0.2
