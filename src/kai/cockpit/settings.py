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

    # Public base URL the cockpit uses when minting magic-link emails.
    # Empty when unset — callers decide what to do.
    public_url: str = ""
    contact_email: str = "hello@meetk.ai"

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
