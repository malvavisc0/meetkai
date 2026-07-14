"""Email bot transport settings — read from KAI_BOT_* env vars."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class EmailSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KAI_BOT_", env_file=".env", extra="ignore")

    control_host: str = "0.0.0.0"
    control_port: int  # injected by cockpit at start time
    hmac_key: str  # /ingest route verifies with this
    hmac_algorithm: str = "sha512"


def get_email_settings() -> EmailSettings:
    return EmailSettings()  # type: ignore[call-arg]
