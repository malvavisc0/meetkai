import logging
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Core framework settings. Loaded from KAI_* env vars / .env.

    Contains only settings shared across all bots: LLM, agent, logging.
    Transport-specific settings (WAHA, Telegram, …) live in their bot packages.
    """

    model_config = SettingsConfigDict(env_prefix="KAI_", env_file=".env", extra="ignore")

    llm_api_base: str = Field(
        default="https://api.openai.com/v1", description="OpenAI-like API base URL"
    )
    llm_api_key: str = Field(default="", description="API key for LLM")
    llm_model: str = Field(default="gpt-4o-mini", description="LLM model name")
    llm_enable_thinking: bool = Field(
        default=False,
        description="Enable thinking/reasoning blocks in LLM responses",
    )

    agent_max_history_messages: int = Field(
        default=100,
        description="Maximum stored chat messages per conversation",
    )
    agent_max_history_chars: int = Field(
        default=12000,
        description="Approximate maximum stored chat history characters per conversation",
    )
    agent_max_conversations: int = Field(
        default=256,
        description="Maximum number of distinct conversations retained in memory (LRU eviction)",
    )
    agent_history_folder: Path | None = Field(
        default=Path("data"),
        description="Directory for per-bot chat history. Null = no persistence.",
    )

    agent_language: str = Field(
        default="English",
        description="Default language for the agent",
    )
    agent_language_explicit: bool = Field(
        default=False,
        description="True when agent_language was set via env or CLI",
    )

    tasks_enabled: bool = Field(
        default=True,
        description="Enable the reminder/task scheduler for bots that support it",
    )
    tasks_poll_interval_seconds: float = Field(
        default=5.0,
        description="How often (seconds) the scheduler checks for due tasks",
    )
    tasks_folder: Path = Field(
        default=Path("/app/data"),
        description="Directory for per-bot task stores (must be an absolute path).",
    )
    escalations_folder: Path = Field(
        default=Path("/app/data"),
        description="Directory for per-bot escalation stores (must be an absolute path).",
    )

    @field_validator("tasks_folder", "escalations_folder")
    @classmethod
    def validate_store_folder_absolute(cls, v: Path) -> Path:
        if not v.is_absolute():
            raise ValueError(
                f"{v} is not an absolute path. KAI_TASKS_FOLDER and "
                "KAI_ESCALATIONS_FOLDER must be absolute paths."
            )
        return v

    cockpit_url: str = Field(
        default="",
        description=(
            "Base URL of the cockpit web app, for bot escalation forwarding. Empty = no forwarding."
        ),
    )

    cockpit_escalation_secret: str = Field(
        default="",
        description="Shared secret for the bot escalation webhook. Empty = no auth.",
    )

    log_dir: Path = Field(
        default=Path("data/kai/logs"),
        description="Directory for log files",
    )

    configs_dir: Path = Field(
        default=Path("configs"),
        description="Directory for per-bot external config overrides",
    )

    @field_validator("llm_api_base")
    @classmethod
    def validate_llm_api_base(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"llm_api_base must start with http:// or https://, got: {v}")
        if not parsed.netloc:
            raise ValueError(f"llm_api_base must include a host, got: {v}")
        return v.rstrip("/")

    @field_validator("agent_max_history_messages")
    @classmethod
    def validate_agent_max_history_messages(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"agent_max_history_messages must be >= 0, got: {v}")
        return v

    @field_validator("agent_max_history_chars")
    @classmethod
    def validate_agent_max_history_chars(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"agent_max_history_chars must be >= 0, got: {v}")
        return v

    def validate_startup(self) -> list[str]:
        """Return startup warnings (non-fatal). A missing key disables the tool."""
        warnings: list[str] = []
        if self.llm_api_key in ("sk-placeholder", ""):
            warnings.append("KAI_LLM_API_KEY is not set")
        for w in warnings:
            logger.warning(w)
        return warnings

    @classmethod
    def for_test(cls, **overrides: object) -> "Settings":
        return cls(_env_file=None, **overrides)  # type: ignore[call-arg]


def get_settings() -> Settings:
    return Settings()
