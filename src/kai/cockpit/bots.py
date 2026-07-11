"""BotType registry: supported feature flags + settings per bot type.

The registry is the single source of truth for what the settings form renders.
Adding a new bot type later = add an entry here + a settings form schema.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BotType:
    name: str
    feature_flags: list[str]
    required_settings: list[str] = field(default_factory=list)
    description: str = ""
    default_goal: str = ""
    # Connection services a deployment of this type must have to start.
    # The start gate checks each is present and connected; missing any
    # raises ConnectionRequiredError.
    required_connections: list[str] = field(default_factory=list)
    # Connection services this bot type can optionally use when the
    # operator enables them on a specific deployment via
    # Deployment.settings["tools"]. A bot may only enable a service listed
    # here — the settings UI never offers a toggle the bot can't use.
    supported_connections: list[str] = field(default_factory=list)


BOT_TYPES: dict[str, BotType] = {
    "waha": BotType(
        name="waha",
        feature_flags=["image", "stt", "tts", "video"],
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
        required_connections=["whatsapp"],
        # Forward declaration: the `database` connection service ships in
        # Fix 05. Declaring it now makes the catalog the single source of
        # truth so the settings form can already offer the toggle (disabled
        # until the connection exists). `email` is deliberately not added
        # here — no bot type consumes it yet.
        supported_connections=["database"],
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


@dataclass(frozen=True)
class CredentialField:
    name: str
    label: str
    type: str  # "text" | "secret" | "int" | "bool"
    required: bool = True
    help: str = ""


@dataclass(frozen=True)
class CredentialType:
    """Settings-form shape for one credential connection type.

    Pure data: enough for a single generic settings-form renderer + save
    handler (Fixes 05/06) without generalizing how the bot uses the
    credential. ``secret_fields`` is the list Fix 03's encrypt/decrypt and
    R5's mask-on-render hook into — membership is automatic, not per-type
    opt-in.
    """

    service: str
    label: str
    fields: list[CredentialField]
    secret_fields: list[str] = field(default_factory=list)
    testable: bool = False


CREDENTIAL_TYPES: dict[str, CredentialType] = {
    "database": CredentialType(
        service="database",
        label="Database",
        fields=[
            CredentialField("label", "Label", "text", required=True),
            CredentialField("url", "Connection URL", "secret", required=True),
        ],
        secret_fields=["url"],
        testable=True,
    ),
    "smtp": CredentialType(
        service="smtp",
        label="Email (SMTP)",
        fields=[
            CredentialField("host", "Host", "text", required=True),
            CredentialField("port", "Port", "int", required=True),
            CredentialField("username", "Username", "text", required=True),
            CredentialField("password", "Password", "secret", required=True),
            CredentialField("from_address", "From address", "text", required=True),
            CredentialField("use_tls", "Use TLS", "bool", required=False),
        ],
        secret_fields=["password"],
        testable=True,
    ),
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
