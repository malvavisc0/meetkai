"""Cockpit process configuration — loaded from ``KAI_*`` env vars / ``.env``.

Distinct from per-bot transport settings and the core agent ``Settings``.
Covers the cockpit's own DB, session secret, public URL, SMTP relay for
login links, and behaviour flags.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class CockpitSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KAI_", env_file=".env", extra="ignore")

    cockpit_secret: str = ""
    cockpit_db: str = "sqlite:///data/cockpit.db"
    cockpit_testing: bool = False
    cockpit_auto_approve_login: bool = False
    public_url: str = ""
    contact_email: str = "hello@meetk.ai"
    cockpit_internal_url: str = "http://127.0.0.1:8080"
    cockpit_escalation_secret: str = ""
    escalations_path: Path = Path("data/cockpit.escalations.json")
    waha_webhook_port_range: str = "8100-8199"
    smtp_host: str = "mailpit"
    smtp_port: int = 1025
    smtp_from: str = "kai@dev"
    smtp_user: str = ""
    smtp_password: str = ""


def get_cockpit_settings() -> CockpitSettings:
    return CockpitSettings()
