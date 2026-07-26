import logging

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class MediaSettings(BaseSettings):
    """STT/TTS media-service settings (whisper, kokoro, ffmpeg).

    These are transport-agnostic (they configure the shared whisper-server /
    kokoro server the cockpit spawns, not the WAHA HTTP API). Only the WAHA
    bot currently exercises STT/TTS, but any bot could in the future.
    """

    model_config = SettingsConfigDict(env_prefix="KAI_MEDIA_", env_file=".env", extra="ignore")

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
    kokoro_voice_map: str = Field(
        default="",
        description=(
            "Per-language Kokoro voice overrides as 'lang=voice,lang=voice' "
            "(e.g. 'es=ef_dora,fr-fr=ff_siwis'). Unmapped languages use the "
            "built-in default voice for that language. Voice replies detect "
            "the reply's language at synthesis time regardless of this setting."
        ),
    )
    kokoro_max_chars: int = Field(
        default=300,
        description=(
            "Max reply length (chars) eligible for voice synthesis; longer replies stay text"
        ),
    )
    kokoro_server_host: str = Field(default="127.0.0.1", description="Kokoro TTS server host")
    kokoro_server_port: int = Field(default=8788, description="Kokoro TTS server port")
    ready_timeout: float = Field(
        default=30.0,
        description=(
            "Seconds an operator's manual deployment start() waits for MEDIA_READY "
            "before failing (bounded gate; see MediaServiceManager.wait_ready)"
        ),
    )

    @classmethod
    def for_test(cls, **overrides: object) -> "MediaSettings":
        """Construct MediaSettings for tests without loading ``.env``/env vars."""
        return cls(_env_file=None, **overrides)  # type: ignore[call-arg]


def get_media_settings() -> MediaSettings:
    return MediaSettings()
