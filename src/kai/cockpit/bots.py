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


BOT_TYPES: dict[str, BotType] = {
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

# Every language a deployment's `language` field may take
# (server-validated in DeploymentsService.create/edit —
# the form <select> alone is never trusted).
ALL_LANGUAGES: tuple[str, ...] = ("English", "French", "Italian", "Portuguese", "Spanish")


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

# Display label per connection service.
# Ingress-only connections (resend) come from WEBHOOK_CONNECTION_TYPES.
CONNECTION_LABELS: dict[str, str] = {
    **{service: ct.label for service, ct in CREDENTIAL_TYPES.items()},
    **{service: wt.label for service, wt in WEBHOOK_CONNECTION_TYPES.items()},
}


# Capability display names, shared by the Runtime overview
# badges and the settings checkboxes — one dict so
# both pages can never show different wording for the
# same capability.
CAPABILITY_LABELS: dict[str, str] = {
    "vision": "Vision",
    "image": "Vision",
}
