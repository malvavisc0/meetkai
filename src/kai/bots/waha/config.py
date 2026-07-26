import logging
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class WahaSettings(BaseSettings):
    """WAHA-specific settings. Loaded from KAI_WAHA_* env vars / .env.

    These are owned by the waha bot plugin — not by the core framework.
    Other bots (email, Telegram, …) have their own transport settings.
    """

    model_config = SettingsConfigDict(env_prefix="KAI_WAHA_", env_file=".env", extra="ignore")

    url: str = Field(default="http://localhost:3000", description="WAHA API base URL")
    api_key: str = Field(default="", description="WAHA API key (X-Api-Key header)")
    session: str = Field(default="default", description="WAHA session name")

    webhook_port: int = Field(default=8000, description="Local webhook server port")
    webhook_host: str = Field(default="0.0.0.0", description="Webhook server bind host")
    webhook_public_host: str = Field(
        default="", description="Public hostname for WAHA webhook (e.g., 192.168.1.254)"
    )
    webhook_path: str = Field(default="/webhook/waha", description="Webhook endpoint path")
    hmac_key: str = Field(
        description=(
            "HMAC key for webhook verification (KAI_WAHA_HMAC_KEY). Mandatory: "
            "the webhook and the operator /tell route share this secret, so an "
            "unset key is a startup error rather than a soft warning."
        ),
    )
    hmac_algorithm: str = Field(default="sha512", description="HMAC algorithm (sha256 or sha512)")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"url must start with http:// or https://, got: {v}")
        if not parsed.netloc:
            raise ValueError(f"url must include a host, got: {v}")
        return v.rstrip("/")

    @field_validator("webhook_port")
    @classmethod
    def validate_webhook_port(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError(f"webhook_port must be 1-65535, got: {v}")
        return v

    @field_validator("webhook_path")
    @classmethod
    def validate_webhook_path(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(f"webhook_path must start with /, got: {v}")
        return v

    @field_validator("session")
    @classmethod
    def validate_session(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("session cannot be empty")
        return v.strip()

    def validate_startup(self) -> list[str]:
        warnings: list[str] = []
        if self.api_key == "":
            warnings.append("KAI_WAHA_API_KEY is not set — WAHA requests may fail")
        # Defense-in-depth: the field is required, so a missing key fails at
        # construction (pydantic ValidationError). This explicit check guards
        # against the field ever being relaxed back to optional.
        if not self.hmac_key:
            warnings.append("KAI_WAHA_HMAC_KEY is not set — webhook + /tell are unauthenticated")
        for w in warnings:
            logger.warning(w)
        return warnings

    @classmethod
    def for_test(cls, **overrides: object) -> "WahaSettings":
        """Construct WahaSettings for tests without loading ``.env``/env vars.

        Centralizes the one pydantic-settings/pyright stub gap (the
        private ``_env_file`` init kwarg isn't part of the generated
        ``__init__`` signature) so individual tests don't each need their
        own ``# type: ignore[call-arg]``. ``hmac_key`` has no default, so
        callers must still pass it explicitly.
        """
        return cls(_env_file=None, **overrides)  # type: ignore[call-arg]


def get_waha_settings() -> WahaSettings:
    # ``hmac_key`` is required (no default) but pydantic ``BaseSettings``
    # fills it from ``KAI_WAHA_HMAC_KEY`` at runtime, which pyright can't
    # model — hence the call-arg ignore, matching the same pattern used in
    # the config tests.
    return WahaSettings()  # type: ignore[call-arg]
