"""Brain settings — LightRAG + crawl4ai connection config + agent instructions.

Loaded from ``KAI_BRAIN_*`` env vars (or ``.env``). These are owned by the
brain integration, not by any single bot: ``cli/bot.py:_start()`` reads them
after ``bot.configure()`` and, when the connection fields are present,
constructs a ``LightRagClient``, registers the ``brain_query`` tool (`BRAIN_TOOL_NAME`), and
injects the operator-authored ``instruction`` into the agent's tool workflow
prompt.

Per-user fields (``workspace``, ``instruction``, ``mandatory``) are NOT set
in ``.env`` — they live on the user's ``Connection.config`` JSON (row with
``service="lightrag"``) and are injected into the bot subprocess env at
runtime by ``deployments.start()``.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class BrainSettings(BaseSettings):
    """LightRAG + crawl4ai settings + per-Brain agent instructions.

    The connection fields (base_url, lightrag_api_key, crawler_url,
    crawl4ai_token) are static — one shared lightrag + one shared crawl4ai
    container for the whole kai deployment. The per-user fields (workspace,
    instruction, mandatory) are injected at runtime from the Connection row.
    """

    model_config = SettingsConfigDict(env_prefix="KAI_BRAIN_", env_file=".env", extra="ignore")

    # --- LightRAG (the vector/graph store) ---
    base_url: str = Field(
        default="",
        description="LightRAG API base URL (e.g. http://lightrag:9621).",
    )
    lightrag_api_key: str = Field(
        default="",
        description="LightRAG X-API-Key (KAI_BRAIN_LIGHTRAG_API_KEY).",
    )
    workspace: str = Field(
        default="default",
        description=(
            "LightRAG workspace — the user's Brain isolation key. Injected "
            "per-user by deployments.start() (kai-v001-<email>), NOT in .env."
        ),
    )

    # --- crawl4ai (the headless-browser crawler) ---
    crawler_url: str = Field(
        default="",
        description="crawl4ai API base URL (e.g. http://crawl4ai:11235).",
    )
    crawl4ai_token: str = Field(
        default="",
        description="crawl4ai bearer token (KAI_BRAIN_CRAWL4AI_TOKEN).",
    )

    # --- crawl BFS bounds (kai-orchestrated, see crawler.py) ---
    crawl_max_depth: int = Field(
        default=1,
        description=(
            "Max BFS depth for whole-site crawl (seed = 0). Depth 1 fetches "
            "the seed page plus its directly linked same-host pages. crawl4ai "
            "docs warn >3 grows exponentially."
        ),
    )
    crawl_max_pages: int = Field(
        default=25,
        description="Hard cap on pages fetched per whole-site crawl.",
    )

    # --- Per-Brain agent instructions (injected from Connection.config) ---
    instruction: str = Field(
        default="",
        description=(
            "Operator-authored guidance for when the bot should use "
            "brain_query (one trigger per line). Empty = no instruction "
            "injected; the tool is still available, the agent decides on its "
            "own. Injected by deployments.start() as KAI_BRAIN_INSTRUCTION."
        ),
    )
    mandatory: bool = Field(
        default=False,
        description=(
            "If true, the workflow prompt uses MUST instead of SHOULD — the "
            "operator asserts the Brain is required for this bot's answers, and "
            "the prompt instructs the model to call brain_query first, fall "
            "back to web_search when the Brain has no relevant answer, and "
            "never answer factual questions from its own training data. This is "
            "strong steering (MUST wording + greedy decoding via "
            "mandatory_temperature), NOT a code-level guarantee that every "
            "answer was grounded."
        ),
    )
    mandatory_temperature: float = Field(
        default=0.0,
        description=(
            "LLM temperature applied when mandatory=True. Lower temperatures "
            "make the model more likely to follow the MUST instruction to call "
            "brain_query first. Applied via KaiAgent.set_temperature. 0 = "
            "greedy decoding; set higher (e.g. 0.3) if deterministic replies "
            "feel too robotic."
        ),
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
        """Return startup warnings (non-fatal). A missing key means the
        Brain tool simply isn't registered; this never blocks bot startup."""
        warnings: list[str] = []
        if not self.base_url:
            warnings.append("KAI_BRAIN_BASE_URL is not set — brain tool disabled")
        if not self.lightrag_api_key:
            warnings.append("KAI_BRAIN_LIGHTRAG_API_KEY is not set — brain tool disabled")
        if not self.crawler_url:
            warnings.append("KAI_BRAIN_CRAWLER_URL is not set — website ingest disabled")
        if not self.crawl4ai_token:
            warnings.append("KAI_BRAIN_CRAWL4AI_TOKEN is not set — website ingest disabled")
        for w in warnings:
            logger.warning(w)
        return warnings

    @property
    def brain_enabled(self) -> bool:
        """True when the lightrag connection is fully configured (base_url +
        api_key present). The brain_query tool is registered only when this
        is true."""
        return bool(self.base_url and self.lightrag_api_key)

    def workflow_instruction(self) -> str:
        """Build the agent tool-workflow prompt block from the operator's
        ``instruction`` + ``mandatory`` flag. Always includes a general
        Brain-awareness sentence (the agent should know the tool exists and
        what it's for even if the operator hasn't written specific
        triggers); the operator's free-text triggers, if any, are appended
        as a bulleted list.
        """
        return build_brain_workflow_instruction(self.instruction, self.mandatory)


# The agent-facing tool name. Kept as a single constant so the prompt text
# below and the actual ``FunctionTool`` registration (see
# ``agent/tools/brain.py``) never
# drift out of sync with each other.
BRAIN_TOOL_NAME = "brain_query"


def build_brain_workflow_instruction(instruction: str, mandatory: bool) -> str:
    """Render the operator's per-Brain instruction into the agent prompt.

    ``instruction`` is free text, one trigger per line (as the operator enters
    it in the Brain UI). The general "you have a Brain" framing is always
    included as long as this function is called at all (i.e. the Brain is
    connected) — only the specific trigger bullets are conditional on the
    operator having written any. The verb is MUST when ``mandatory`` else
    SHOULD, and when ``mandatory`` a grounding rule is appended: call the
    Brain first, fall back to ``web_search`` if it has nothing, and never
    answer facts from training data.
    """
    triggers = [ln.strip() for ln in instruction.splitlines() if ln.strip()]
    intro = (
        "You have access to a Brain: a knowledge base of operator-provided, "
        "fact-checked documents. Treat information returned by the Brain as "
        "higher priority than your own training data.\n"
        f"You have a tool called `{BRAIN_TOOL_NAME}` for searching the Brain."
    )
    # When the Brain is mandatory, the model is steered to always ground
    # factual answers: call the Brain first, fall back to the web if it has
    # nothing, and never answer facts from memory. web_search is only actually
    # available when the bot registered it; the final rung ("say you don't
    # know") covers the case where neither source has the answer.
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
