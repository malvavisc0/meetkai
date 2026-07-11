"""Bot-level configuration models and defaults for the WAHA bot.

These are distinct from :mod:`kai.bots.waha.config` which holds the
WAHA *transport* settings (API URL, webhook, whisper, etc.).
"""

from pydantic import BaseModel, ConfigDict, Field

_DEFAULT_PARTICIPATION_RATE = 0.15
_DEFAULT_PARTICIPATION_COOLDOWN = 90.0
_DEFAULT_PARTICIPATION_STREAK_MAX = 2


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
    cooldown_seconds: float = _DEFAULT_PARTICIPATION_COOLDOWN
    streak_max: int = _DEFAULT_PARTICIPATION_STREAK_MAX


class BotConfig(BaseModel):
    trigger_keyword: str = "kai"
    whitelist: list[str] = Field(default_factory=list)
    blacklist: list[str] = Field(default_factory=list)
    language: str = "English"
    timezone: str | None = None
    mentions_enabled: bool = True
    media: MediaConfig = Field(default_factory=MediaConfig)
    participation: ParticipationConfig = Field(default_factory=ParticipationConfig)
