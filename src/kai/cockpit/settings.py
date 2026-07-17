"""Cockpit settings — the management UI + bot spawner's own configuration.

Loaded from ``KAI_*`` env vars / ``.env``. Distinct from the per-bot transport
settings (``WahaSettings``, ``EmailSettings``, …) and the core agent
``Settings``: these cover the cockpit process itself — its DB, session
secret, public URL, contact email, magic-link SMTP relay, and a couple of
behaviour flags.

The cockpit's magic-link relay (``smtp_*`` below) is intentionally separate
from ``SmtpSettings`` (``KAI_SMTP_TOOL_*``): the former is the kai
install's own mail relay for login links; the latter is the operator's
sending account for the agent's ``send_email`` tool.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class CockpitSettings(BaseSettings):
    """Cockpit process configuration, read from ``KAI_*`` env vars / ``.env``."""

    model_config = SettingsConfigDict(env_prefix="KAI_", env_file=".env", extra="ignore")

    # Session cookie signing secret. Empty by default; ``get_cockpit_secret``
    # raises at startup when unset so a misconfigured deploy fails loudly
    # rather than signing cookies with a guessable value.
    cockpit_secret: str = ""
    cockpit_db: str = "sqlite:///data/cockpit.db"
    # Skip startup deployment-reconciliation (tests only).
    cockpit_testing: bool = False
    # When true, magic links are minted and sent immediately on a login
    # request — no manual ``kai cockpit request approve`` needed.
    cockpit_auto_approve_login: bool = False

    # Public base URL the cockpit uses when minting magic-link emails. This is
    # the browser-facing external URL — NOT used for bot→cockpit comms (bots
    # are in-container subprocesses; see cockpit_internal_url below).
    public_url: str = ""
    contact_email: str = "hello@meetk.ai"

    # Internal URL bots use to POST escalations back to the cockpit
    # (/api/escalations). Bots are subprocesses spawned in the cockpit's own
    # container, so the default loopback address reaches the listener
    # regardless of the bind host (0.0.0.0 accepts on 127.0.0.1). Override
    # only for non-standard ports or a separate-bot-container topology.
    cockpit_internal_url: str = "http://127.0.0.1:8080"

    # Shared secret for the bot→cockpit escalation webhook. When set, the bot
    # sends it as ``Authorization: Bearer <secret>`` and the cockpit's
    # POST /api/escalations rejects requests without it. Empty (default) =
    # no auth — the cockpit is behind a reverse proxy / private network (the
    # same trust model as the existing read-only /api routes).
    cockpit_escalation_secret: str = ""

    # Cockpit's aggregated escalation store (bots POST to /api/escalations,
    # which writes here). Separate from the per-bot escalations_folder.
    escalations_path: Path = Path("data/cockpit.escalations.json")

    # Port range the cockpit allocates per-user WAHA webhook listeners from.
    # Purely internal to the compose network — no host port publishing needed.
    waha_webhook_port_range: str = "8100-8199"

    # --- Magic-link SMTP relay (the cockpit's own login-link mailer) ---
    smtp_host: str = "mailpit"
    smtp_port: int = 1025
    smtp_from: str = "kai@dev"
    smtp_user: str = ""
    smtp_password: str = ""


def get_cockpit_settings() -> CockpitSettings:
    return CockpitSettings()
