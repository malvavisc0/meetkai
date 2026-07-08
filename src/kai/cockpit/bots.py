"""BotType registry: supported feature flags + settings per bot type.

The registry is the single source of truth for what the settings form renders.
Adding a new bot type later = add an entry here + a settings form schema.
"""

from dataclasses import dataclass, field


@dataclass
class BotType:
    name: str
    feature_flags: list[str]
    settings: list[str]
    required_settings: list[str] = field(default_factory=list)
    description: str = ""


BOT_TYPES: dict[str, BotType] = {
    "waha": BotType(
        name="waha",
        feature_flags=["image", "stt", "tts", "video"],
        settings=[
            "whitelist",
            "blacklist",
            "language",
            "trigger_keyword",
            "mentions_enabled",
            "timezone",
            "participation.enabled",
            "participation.rate",
            "participation.cooldown_seconds",
            "participation.streak_max",
        ],
        required_settings=["language"],
        description=(
            "WhatsApp bot via WAHA. Replies in chats and groups, "
            "handles voice notes, images, and video, and can proactively "
            "participate in conversations."
        ),
    ),
}

LANGUAGE_VOICE_MAP: dict[str, str] = {
    "Spanish": "ef_dora",
    "English": "af_heart",
    "French": "ff_siwis",
    "German": "hf_alpha",
    "Italian": "if_sara",
    "Portuguese": "pf_dora",
}


def auto_pick_voice(language: str) -> str:
    """Return the default kokoro voice for a language, or af_heart as fallback."""
    return LANGUAGE_VOICE_MAP.get(language, "af_heart")
