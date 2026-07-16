"""Email bot transport settings — read from KAI_BOT_* env vars.

Two email-bot feature knobs are injected by the cockpit under ``KAI_EMAIL_*``
rather than ``KAI_BOT_*`` (they predate the prefix and are surfaced as
bot-specific feature flags), so they use an explicit alias instead of the
class env_prefix.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EmailSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KAI_BOT_", env_file=".env", extra="ignore", populate_by_name=True
    )

    control_host: str = "0.0.0.0"
    control_port: int  # set by the cockpit when starting the bot
    hmac_key: str  # used to verify incoming webhooks
    hmac_algorithm: str = "sha512"

    # Per-deployment image-vision flag, injected by the cockpit at start time
    # from Deployment.feature_flags["image"].
    vision: bool = Field(
        default=False,
        validation_alias=AliasChoices("KAI_EMAIL_VISION"),
    )
    # Cap (bytes) for downloaded attachments; larger downloads are skipped.
    max_attachment_bytes: int = Field(
        default=10 * 1024 * 1024,
        validation_alias=AliasChoices("KAI_EMAIL_MAX_ATTACHMENT_BYTES"),
    )


def get_email_settings() -> EmailSettings:
    return EmailSettings()  # type: ignore[call-arg]
