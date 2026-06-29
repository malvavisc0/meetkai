"""Bot-level configuration dataclasses and defaults for the WAHA bot.

These are distinct from :mod:`kai.bots.waha.config` which holds the
WAHA *transport* settings (API URL, webhook, whisper, etc.).
"""

from dataclasses import dataclass, field

_DEFAULT_PARTICIPATION_RATE = 0.15
_DEFAULT_PARTICIPATION_COOLDOWN = 90.0
_DEFAULT_PARTICIPATION_STREAK_MAX = 2


@dataclass
class MediaConfig:
    image_enabled: bool = True
    voice_enabled: bool = True
    instagram_enabled: bool = True
    max_size_mb: int = 10


@dataclass
class ParticipationConfig:
    enabled: bool = True
    rate: float = _DEFAULT_PARTICIPATION_RATE
    cooldown_seconds: float = _DEFAULT_PARTICIPATION_COOLDOWN
    streak_max: int = _DEFAULT_PARTICIPATION_STREAK_MAX


@dataclass
class BotConfig:
    trigger_keyword: str = "kai"
    whitelist: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)
    language: str = "English"
    timezone: str | None = None
    mentions_enabled: bool = True
    media: MediaConfig = field(default_factory=MediaConfig)
    participation: ParticipationConfig = field(default_factory=ParticipationConfig)
