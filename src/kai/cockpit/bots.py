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
    # Name of a template under templates/icons/*.svg (without the extension),
    # shown next to this bot type in the console's bot-type picker,
    # deployment cards, and the deploy wizard header.
    icon: str = "bot"


BOT_TYPES: dict[str, BotType] = {
    "waha": BotType(
        name="waha",
        feature_flags=["image", "stt", "tts", "video"],
        required_settings=["language"],
        description=(
            "Integrate an AI assistant directly into your WhatsApp. "
            "It keeps track of context for smarter replies and responds "
            "instantly when is need it. Support for images, voice notes, "
            "and video means the bot can interact naturally."
        ),
        default_goal=(
            "Be warm, useful, and concise. Answer what you can from what you "
            "know, ask before guessing, and only reply when you add value."
        ),
        required_connections=["whatsapp"],
        # Forward declaration: the `database` connection service ships in
        # Fix 05, `smtp` in Fix 06. Declaring them here makes the catalog
        # the single source of truth so the settings form can offer the
        # toggle (disabled until the connection exists).
        supported_connections=["database", "smtp"],
        icon="message-circle",
    ),
    "email": BotType(
        name="email",
        feature_flags=["image"],
        required_settings=["language"],
        description=(
            "A support bot that answers questions via email, grounded in "
            "your Brain — powered by Resend inbound webhooks and an SMTP "
            "reply path."
        ),
        default_goal=(
            "Answer support questions grounded in the connected Brain. Be "
            "helpful, concise, and honest about limitations. If the Brain "
            "doesn't have the answer, say so instead of guessing."
        ),
        required_connections=["resend", "smtp"],
        supported_connections=["database"],
        icon="mail",
    ),
}

LANGUAGE_VOICE_MAP: dict[str, str] = {
    "Spanish": "ef_dora",
    "English": "af_heart",
    "French": "ff_siwis",
    "Italian": "if_sara",
    "Portuguese": "pf_dora",
}

# Languages the LLM can be told to reply in, beyond the ones Kokoro v1.0 has
# a voice for (LANGUAGE_VOICE_MAP above). German has no Kokoro v1.0 voice at
# all — a bot configured for German still replies in German text; it just
# never gets voice notes (see kai.bots.waha.tts.resolve_kokoro_lang). Do NOT
# add German (or any other Kokoro-unsupported language) to
# LANGUAGE_VOICE_MAP with a made-up voice — that previously mapped German to
# "hf_alpha" (a Hindi voice), which made German voice replies get
# synthesized with English phonemization in a Hindi voice.
AGENT_ONLY_LANGUAGES: tuple[str, ...] = ("German",)


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


@dataclass(frozen=True)
class WebhookConnectionType:
    """Settings-form shape for an ingress-only connection type.

    A webhook connection carries the secrets the cockpit uses to verify and
    parse inbound provider webhooks at the centralized ingress route (Path
    2): a signing secret for ``verify_signature``, and — for providers whose
    webhook body omits the message content (Resend's inbound webhook carries
    only envelope metadata, no body/attachments) — an API key ``parse`` uses
    to fetch it. The bot itself never receives either secret — verification
    and enrichment happen in the cockpit, not the subprocess — so unlike
    ``CredentialType`` nothing here is injected as env into the bot.
    ``secret_fields`` is the same hook Fix 03's encrypt/decrypt and the
    mask-on-render path use.

    ``webhook_type`` is the key this connection verifies for in
    ``WEBHOOK_TYPES`` (added in 03): one webhook connection type maps to one
    webhook verify/parse contract. ``testable`` mirrors ``CredentialType`` so
    the existing "Test connection" affordance renders (the resend connection
    has a self-loopback test).
    """

    service: str
    label: str
    fields: list[CredentialField]
    webhook_type: str  # key in WEBHOOK_TYPES this connection verifies for
    secret_fields: list[str] = field(default_factory=list)
    testable: bool = True


WEBHOOK_CONNECTION_TYPES: dict[str, WebhookConnectionType] = {
    "resend": WebhookConnectionType(
        service="resend",
        label="Email Inbox (Resend)",
        fields=[
            CredentialField("signing_secret", "Signing secret", "secret", required=True),
            # Resend's inbound webhook carries no body/attachment content —
            # only envelope metadata (see webhooks.py:_parse_resend). The
            # API key fetches the body via the Received Emails API and
            # attachment download URLs via the Attachments API.
            CredentialField("api_key", "API key", "secret", required=True),
        ],
        secret_fields=["signing_secret", "api_key"],
        webhook_type="resend",
    ),
}

# Display label for every connection service a BotType can declare in
# required_connections/supported_connections. WhatsApp isn't a
# CredentialType (it's provisioned via WAHA, not a generic credential
# form), so it needs its own entry rather than living in CREDENTIAL_TYPES.
# Ingress-only connections (resend) come from WEBHOOK_CONNECTION_TYPES.
CONNECTION_LABELS: dict[str, str] = {
    "whatsapp": "WhatsApp",
    **{service: ct.label for service, ct in CREDENTIAL_TYPES.items()},
    **{service: wt.label for service, wt in WEBHOOK_CONNECTION_TYPES.items()},
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
