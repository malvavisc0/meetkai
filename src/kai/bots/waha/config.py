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
    hmac_key: str = Field(
        description=(
            "HMAC key for webhook verification (KAI_WAHA_HMAC_KEY). Mandatory: "
            "the webhook and the operator /tell route share this secret, so an "
            "unset key is a startup error rather than a soft warning."
        ),
    )
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

    kokoro_enabled: bool = Field(default=True, description="Enable Kokoro TTS voice replies")
    kokoro_model_path: str = Field(
        default="models/kokoro/kokoro-v1.0.int8.onnx", description="Path to Kokoro ONNX model"
    )
    kokoro_voices_path: str = Field(
        default="models/kokoro/voices-v1.0.bin", description="Path to Kokoro voices file"
    )
    kokoro_voice: str = Field(
        default="af_heart",
        description="Kokoro voice name (must match the language — af_*/am_* for English)",
    )
    kokoro_lang: str = Field(
        default="",
        description="Kokoro language code (empty = derive from bot language at startup)",
    )
    kokoro_speed: float = Field(default=1.0, description="Kokoro speech speed multiplier (0.5–2.0)")
    kokoro_max_chars: int = Field(
        default=300,
        description=(
            "Max reply length (chars) eligible for voice synthesis; longer replies stay text"
        ),
    )
    kokoro_server_host: str = Field(default="127.0.0.1", description="Kokoro TTS server host")
    kokoro_server_port: int = Field(default=8788, description="Kokoro TTS server port")
    media_ready_timeout: float = Field(
        default=30.0,
        description=(
            "Seconds an operator's manual deployment start() waits for MEDIA_READY "
            "before failing (bounded gate; see MediaServiceManager.wait_ready)"
        ),
    )

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


def get_waha_settings() -> WahaSettings:
    # ``hmac_key`` is required (no default) but pydantic ``BaseSettings``
    # fills it from ``KAI_WAHA_HMAC_KEY`` at runtime, which pyright can't
    # model — hence the call-arg ignore, matching the same pattern used in
    # the config tests.
    return WahaSettings()  # type: ignore[call-arg]
