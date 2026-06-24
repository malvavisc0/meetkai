import logging
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class WahaSettings(BaseSettings):
    """WAHA-specific settings. Loaded from KAI_WAHA_* env vars / .env.

    These are owned by the waha bot plugin — not by the core framework.
    Other bots (email, Telegram, …) have their own transport settings.
    """

    model_config = {"env_prefix": "KAI_WAHA_", "env_file": ".env", "extra": "ignore"}

    url: str = Field(default="http://localhost:3000", description="WAHA API base URL")
    api_key: str = Field(default="", description="WAHA API key (X-Api-Key header)")
    session: str = Field(default="default", description="WAHA session name")

    webhook_port: int = Field(default=8000, description="Local webhook server port")
    webhook_host: str = Field(default="0.0.0.0", description="Webhook server bind host")
    webhook_public_host: str = Field(
        default="", description="Public hostname for WAHA webhook (e.g., 192.168.1.254)"
    )
    webhook_path: str = Field(default="/webhook/waha", description="Webhook endpoint path")
    hmac_key: str | None = Field(default=None, description="HMAC key for webhook verification")
    hmac_algorithm: str = Field(default="sha512", description="HMAC algorithm (sha256 or sha512)")

    ffmpeg_path: str = Field(default="vendor/ffmpeg/ffmpeg", description="Path to ffmpeg binary")
    whisper_cpp_path: str = Field(
        default="vendor/whisper.cpp/whisper-cli", description="Path to whisper.cpp binary"
    )
    whisper_model_path: str = Field(
        default="models/whisper/ggml-base.bin", description="Path to whisper GGML model"
    )
    whisper_language: str = Field(
        default="auto", description="Language for whisper transcription (auto = detect)"
    )
    whisper_server_mode: bool = Field(
        default=True, description="Run whisper-server instead of per-request CLI"
    )
    whisper_server_host: str = Field(default="127.0.0.1", description="Whisper server bind host")
    whisper_server_port: int = Field(default=8787, description="Whisper server port")
    whisper_server_threads: int = Field(default=4, description="Whisper server thread count")

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
        for w in warnings:
            logger.warning(w)
        return warnings


def get_waha_settings() -> WahaSettings:
    return WahaSettings()
