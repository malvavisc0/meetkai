"""BotType registry: feature flags, connections, and per-bot-type metadata.

Adding a bot type: add an entry to BOT_TYPES plus a settings template.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BotType:
    name: str
    feature_flags: list[str]
    required_settings: list[str] = field(default_factory=list)
    description: str = ""
    default_goal: str = ""
    required_connections: list[str] = field(default_factory=list)
    # Optional connections an operator may enable
    # via Deployment.settings["tools"] — only these
    # appear as toggles in the settings form.
    supported_connections: list[str] = field(default_factory=list)
    icon: str = "bot"
    # Whether this bot type implements per-chat sleep/wake
    # (waha only). When False, /sleep and /wake 404.
    supports_sleep: bool = False


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
        # database/smtp/calcom: shipped but optional,
        # declared as single source of truth so the
        # settings form can toggle them.
        supported_connections=["database", "smtp", "calcom"],
        icon="message-circle",
        supports_sleep=True,
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
        supported_connections=["database", "calcom"],
        icon="mail",
    ),
}

LANGUAGE_VOICES: dict[str, list[str]] = {
    # Index 0 is the default voice (used by auto_pick_voice).
    # French has no male voice in Kokoro v1.0.
    "Spanish": ["ef_dora", "em_alex"],
    "English": ["af_heart", "am_michael"],
    "French": ["ff_siwis"],
    "Italian": ["if_sara", "im_nicola"],
    "Portuguese": ["pf_dora", "pm_alex"],
}

VOICE_LABELS: dict[str, str] = {
    "af_heart": "Heart",
    "am_michael": "Michael",
    "ef_dora": "Dora",
    "em_alex": "Alex",
    "ff_siwis": "Siwis",
    "if_sara": "Sara",
    "im_nicola": "Nicola",
    "pf_dora": "Dora",
    "pm_alex": "Alex",
}

# Every language a deployment's `language` field may take
# (server-validated in DeploymentsService.create/edit —
# the form <select> alone is never trusted).
ALL_LANGUAGES: tuple[str, ...] = tuple(sorted(LANGUAGE_VOICES.keys()))

ALL_VOICES: tuple[str, ...] = tuple(
    sorted({voice for voices in LANGUAGE_VOICES.values() for voice in voices})
)

# Voice code -> language. Filters the voice <select>
# to matching languages (client-side in cockpit.js,
# server-side in DeploymentsService).
VOICE_LANGUAGE_BY_CODE: dict[str, str] = {
    voice: lang for lang, voices in LANGUAGE_VOICES.items() for voice in voices
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

    Pure data: enough for the generic settings-form renderer + save handler
    without coupling to how the bot uses the credential. ``secret_fields``
    drives encrypt/decrypt and mask-on-render automatically.
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
    "calcom": CredentialType(
        service="calcom",
        label="Cal.com",
        fields=[
            CredentialField("api_key", "API key", "secret", required=True),
            CredentialField(
                "base_url",
                "API base URL",
                "text",
                required=False,
                help="Defaults to https://api.cal.com/v2 — override for a self-hosted instance.",
            ),
        ],
        secret_fields=["api_key"],
        testable=True,
    ),
}


@dataclass(frozen=True)
class WebhookConnectionType:
    """Settings-form shape for an ingress-only connection type.

    Carries secrets the cockpit uses to verify and parse inbound provider
    webhooks at the centralized ingress route: a signing secret for
    ``verify_signature`` and, for providers whose webhook body omits message
    content (Resend's inbound webhook carries only envelope metadata), an
    API key ``parse`` uses to fetch it. The bot itself never receives
    either secret — verification and enrichment happen in the cockpit, not
    the subprocess. ``secret_fields`` drives encrypt/decrypt and
    mask-on-render, same as CredentialType.

    ``webhook_type`` is the key this connection verifies for in
    ``WEBHOOK_TYPES``: one connection type maps to one webhook verify/parse
    contract.
    """

    service: str
    label: str
    fields: list[CredentialField]
    webhook_type: str  # which webhook type this connection handles
    secret_fields: list[str] = field(default_factory=list)
    testable: bool = True


WEBHOOK_CONNECTION_TYPES: dict[str, WebhookConnectionType] = {
    "resend": WebhookConnectionType(
        service="resend",
        label="Email Inbox (Resend)",
        fields=[
            CredentialField("signing_secret", "Signing secret", "secret", required=True),
            # Resend's inbound webhook carries only envelope metadata;
            # the API key fetches body/attachments via the Resend APIs.
            CredentialField("api_key", "API key", "secret", required=True),
        ],
        secret_fields=["signing_secret", "api_key"],
        webhook_type="resend",
    ),
}

# Display label per connection service. WhatsApp has its own
# entry (provisioned via WAHA, not a credential form);
# ingress-only connections (resend) come from
# WEBHOOK_CONNECTION_TYPES.
CONNECTION_LABELS: dict[str, str] = {
    "whatsapp": "WhatsApp",
    **{service: ct.label for service, ct in CREDENTIAL_TYPES.items()},
    **{service: wt.label for service, wt in WEBHOOK_CONNECTION_TYPES.items()},
}


def auto_pick_voice(language: str) -> str:
    """Return the default (first-listed) Kokoro voice for *language*.

    Raises ValueError for an unsupported language; callers must validate
    the language first.
    """
    if language not in LANGUAGE_VOICES:
        raise ValueError(f"unsupported language: {language!r}. Supported: {ALL_LANGUAGES}")
    return LANGUAGE_VOICES[language][0]


# Capability display names, shared by the Runtime overview
# badges and the settings checkboxes — one dict so
# both pages can never show different wording for the
# same capability.
CAPABILITY_LABELS: dict[str, str] = {
    "vision": "Vision",
    "image": "Vision",
    "video": "Video",
    "voice_to_text": "Speech to text",
    "stt": "Speech to text",
    "text_to_voice": "Text to speech",
    "tts": "Text to speech",
}
