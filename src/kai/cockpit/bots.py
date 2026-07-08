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
    default_goal: str = ""


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
            "WhatsApp Bot. Replies in chats and groups and can proactively "
            "participate in conversations. Media and voice capabilities "
            "depend on the enabled feature flags."
        ),
        default_goal=(
            "Be warm, useful, and concise. Answer what you can from what you "
            "know, ask before guessing, and only reply when you add value."
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


# Single source of truth for capability display names, shared by the
# Runtime overview badges (deployment.html, keyed by the bot's /status
# capability names) and the Parameters checkboxes (settings.html, keyed by
# BotType.feature_flags names) — one dict covering both vocabularies so the
# two pages can never show different wording for the same capability.
CAPABILITY_LABELS: dict[str, str] = {
    "vision": "Vision",
    "image": "Vision",
    "video": "Video",
    "voice_to_text": "Speech to text",
    "stt": "Speech to text",
    "text_to_voice": "Text to speech",
    "tts": "Text to speech",
}
