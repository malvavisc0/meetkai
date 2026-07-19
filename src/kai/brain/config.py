"""Brain settings — Morphik + crawl4ai connection config + agent instructions.

Loaded from ``KAI_BRAIN_*`` env vars (or ``.env``). Per-user fields
(``workspace``, ``instruction``, ``mandatory``) live on the user's
``Connection.config`` JSON and are injected at runtime by ``deployments.start()``.

``workspace`` is the Morphik ``end_user_id`` (the user slug); Morphik enforces
row-level isolation via this field, unlike the previous LightRAG backend which
ignored it.
"""

import logging
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class BrainSettings(BaseSettings):
    """Morphik + crawl4ai settings + per-Brain agent instructions.

    The connection fields (base_url, morphik_token, crawler_url,
    crawl4ai_token) are static — one shared container for the deployment.
    The per-user fields (workspace, instruction, mandatory) are injected at runtime.
    """

    model_config = SettingsConfigDict(env_prefix="KAI_BRAIN_", env_file=".env", extra="ignore")

    base_url: str = Field(
        default="",
        description="Morphik API base URL (e.g. http://morphik:8000).",
    )
    morphik_token: str = Field(
        default="",
        description="Morphik Bearer token (KAI_BRAIN_MORPHIK_TOKEN).",
    )
    workspace: str = Field(
        default="default",
        description="Morphik end_user_id — injected per-user by deployments.start()",
    )

    crawler_url: str = Field(
        default="",
        description="crawl4ai API base URL (e.g. http://crawl4ai:11235).",
    )
    crawl4ai_token: str = Field(
        default="",
        description="crawl4ai bearer token (KAI_BRAIN_CRAWL4AI_TOKEN).",
    )

    crawl_max_depth: int = Field(
        default=1,
        description="Max BFS depth for whole-site crawl. crawl4ai warns >3 grows exponentially.",
    )
    crawl_max_pages: int = Field(
        default=25,
        description="Hard cap on pages fetched per whole-site crawl.",
    )

    instruction: str = Field(
        default="",
        description="Operator-authored guidance for when to use brain_query. One trigger per line.",
    )
    mandatory: bool = Field(
        default=False,
        description="If true, the Brain MUST be called first; greedy decoding is applied.",
    )
    mandatory_temperature: float = Field(
        default=0.0,
        description="LLM temperature applied when mandatory=True. 0 = greedy.",
    )

    @field_validator("base_url", "crawler_url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not v:
            return v  # empty allowed — validate_startup warns
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"url must start with http:// or https://, got: {v}")
        if not parsed.netloc:
            raise ValueError(f"url must include a host, got: {v}")
        return v.rstrip("/")

    @field_validator("crawl_max_depth")
    @classmethod
    def validate_max_depth(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"crawl_max_depth must be >= 0, got {v}")
        return v

    @field_validator("crawl_max_pages")
    @classmethod
    def validate_max_pages(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"crawl_max_pages must be >= 1, got: {v}")
        return v

    def validate_startup(self) -> list[str]:
        """Return startup warnings (non-fatal). A missing token means the
        Brain tool simply isn't registered; this never blocks bot startup."""
        warnings: list[str] = []
        if not self.base_url:
            warnings.append("KAI_BRAIN_BASE_URL is not set — brain tool disabled")
        if not self.morphik_token:
            warnings.append("KAI_BRAIN_MORPHIK_TOKEN is not set — brain tool disabled")
        if not self.crawler_url:
            warnings.append("KAI_BRAIN_CRAWLER_URL is not set — website ingest disabled")
        if not self.crawl4ai_token:
            warnings.append("KAI_BRAIN_CRAWL4AI_TOKEN is not set — website ingest disabled")
        for w in warnings:
            logger.warning(w)
        return warnings

    @property
    def brain_enabled(self) -> bool:
        """True when the morphik connection is fully configured."""
        return bool(self.base_url and self.morphik_token)

    def workflow_instruction(self) -> str:
        """Build the agent tool-workflow prompt block from
        operator's instruction + mandatory flag."""
        return build_brain_workflow_instruction(self.instruction, self.mandatory)

    @classmethod
    def for_test(cls, **overrides: object) -> "BrainSettings":
        return cls(_env_file=None, **overrides)  # type: ignore[call-arg]


BRAIN_TOOL_NAME = "brain_query"


def build_brain_workflow_instruction(instruction: str, mandatory: bool) -> str:
    """Render the operator's per-Brain instruction into the agent prompt."""
    triggers = [ln.strip() for ln in instruction.splitlines() if ln.strip()]
    intro = (
        "You have access to a Brain: a knowledge base of operator-provided, "
        "fact-checked documents. Treat information returned by the Brain as "
        "higher priority than your own training data.\n"
        f"You have a tool called `{BRAIN_TOOL_NAME}` for searching the Brain."
    )
    mandatory_rule = (
        " Because the Brain is mandatory for this bot, you MUST call "
        f"`{BRAIN_TOOL_NAME}` before answering any factual question. If the "
        "Brain returns no relevant information, fall back to `web_search` (when "
        "available) to find a grounded answer. Never answer a factual question "
        "from your own training data; if neither the Brain nor the web has the "
        "answer, say you don't know."
    )
    if not triggers:
        base = (
            f"{intro} Use it whenever a question may be answered by the "
            "operator's uploaded knowledge base."
        )
        return f"{base}{mandatory_rule}" if mandatory else base
    verb = "MUST" if mandatory else "SHOULD"
    body = "\n".join(f"- {ln}" for ln in triggers)
    instruction_block = f"{intro} You {verb} call it when:\n{body}"
    return f"{instruction_block}{mandatory_rule}" if mandatory else instruction_block


def get_brain_settings() -> BrainSettings:
    """Construct BrainSettings from env (cached at call site, not here)."""
    return BrainSettings()
