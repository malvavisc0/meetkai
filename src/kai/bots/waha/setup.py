"""Bot-level configuration models and defaults for the WAHA bot.

These are distinct from :mod:`kai.bots.waha.config` which holds the
WAHA *transport* settings (API URL, webhook, whisper, etc.).
"""

from pydantic import BaseModel, ConfigDict, Field

from kai.agent.tools.email import DEFAULT_DISPLAY_NAME

_DEFAULT_PARTICIPATION_RATE = 0.15
_DEFAULT_PARTICIPATION_COOLDOWN = 90
_DEFAULT_PARTICIPATION_STREAK_MAX = 2

_DEFAULT_VOICE_NOTE_RATE = 0.25
_DEFAULT_VOICE_NOTE_COOLDOWN = 300


class MediaConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    image_enabled: bool = True
    stt_enabled: bool = True
    tts_enabled: bool = True
    video_enabled: bool = True
    instagram_enabled: bool = True
    max_size_mb: int = 10


class ParticipationConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    rate: float = _DEFAULT_PARTICIPATION_RATE
    cooldown_seconds: int = _DEFAULT_PARTICIPATION_COOLDOWN
    streak_max: int = _DEFAULT_PARTICIPATION_STREAK_MAX
    voice_note_rate: float = _DEFAULT_VOICE_NOTE_RATE
    voice_note_cooldown: int = _DEFAULT_VOICE_NOTE_COOLDOWN


class BotConfig(BaseModel):
    trigger_keyword: str = "kai"
    whitelist: list[str] = Field(default_factory=list)
    blacklist: list[str] = Field(default_factory=list)
    language: str = "English"
    timezone: str | None = None
    # The sender identity shown in the "From" header when this deployment
    # uses the bot-agnostic send_email tool (agent/tools/email.py). Mirrors
    # the email bot's own BotConfig.display_name for symmetry — a WAHA
    # deployment sending email presents as its own configured identity
    # rather than a hardcoded literal.
    display_name: str = DEFAULT_DISPLAY_NAME
    mentions_enabled: bool = True
    media: MediaConfig = Field(default_factory=MediaConfig)
    participation: ParticipationConfig = Field(default_factory=ParticipationConfig)
    # LLM sampling temperature (passed to agent.set_temperature() in
    # configure()). Left un-set, the provider's own default applies, which
    # varies by backend and is often not low — explicitly setting it is what
    # makes the model reliably follow the prompt's hard rules (emoji ban,
    # length caps, action protocol) instead of drifting. 0.4 keeps some
    # warmth/spontaneity for the persona while still being well below a
    # provider's typical 0.7-1.0 default.
    temperature: float = 0.4
