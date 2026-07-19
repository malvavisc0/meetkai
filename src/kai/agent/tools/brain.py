"""``brain_query`` — the agent-facing tool over a user's Morphik Brain.

Bot-agnostic by design: no bot module imports this directly. Instead
``cli/bot.py``'s ``_start()`` is the single funnel every bot passes through;
after ``bot.configure()`` it builds a ``MorphikClient`` from
``BrainSettings`` and calls :func:`register_brain_tool` here, which is the
only place that touches both the agent and the Morphik client.

The tool itself is an ``async def`` (the agent's tool dispatch is already
async, so no thread/executor wrapping is needed and no second sync client
class is required). It is registered **without** the
``_logged`` wrapper from ``agent/tools/__init__.py`` because that wrapper is
sync and would break llama_index's coroutine detection — logging is left to
``KaiAgent``'s existing tool-call/tool-result logging in ``agent/core.py``.
"""

import logging
from typing import Protocol, runtime_checkable

from llama_index.core.tools import FunctionTool

from kai.brain.client import QueryMode, QueryResult
from kai.brain.config import BRAIN_TOOL_NAME, build_brain_workflow_instruction

logger = logging.getLogger(__name__)

_MAX_QUERY_CHARS = 2000

_VALID_MODES: frozenset[QueryMode] = frozenset({"naive", "local", "global", "hybrid", "mix"})

# Structural contracts for the two collaborators ``register_brain_tool``
# depends on. Declaring them as Protocols (rather than the concrete
# ``MorphikClient`` / ``KaiAgent``) lets unit tests pass lightweight fakes
# without ``# type: ignore`` while still type-checking the real call sites.
# Both concrete classes structurally satisfy these protocols.


@runtime_checkable
class _BrainQueryClient(Protocol):
    async def query(
        self,
        *,
        query: str,
        workspace: str,
        mode: QueryMode = ...,
    ) -> QueryResult: ...


@runtime_checkable
class _BrainToolAgent(Protocol):
    def register_tool(self, tool: FunctionTool) -> None: ...

    def add_tool_workflow(self, workflow: str | None) -> None: ...


def make_brain_query_tool(
    client: _BrainQueryClient,
    *,
    workspace: str,
    default_mode: QueryMode = "mix",
) -> FunctionTool:
    """Build the ``brain_query`` :class:`FunctionTool`, bound to ``client``.

    ``workspace`` is captured by the closure (one per bot process, set from
    the deployment's Brain ``Connection`` row) — the tool signature exposed
    to the model takes no workspace argument, only ``query``/``mode``.
    """

    async def brain_query(query: str, mode: str = default_mode) -> str:
        """Search the operator's Brain (uploaded knowledge base) and return a grounded answer.

        Use this to answer questions about the organization's products,
        services, policies, or documentation that was uploaded into the
        Brain. Always prefer the Brain's answer over your own training data
        when the two disagree, since the Brain reflects the operator's own
        up-to-date material.

        Args:
            query: The question to search the Brain for. Pass the user's
                actual question (or a focused rephrasing of it), not a
                keyword fragment.
            mode: Retrieval mode — one of "naive", "local", "global",
                "hybrid", "mix" (default "mix", the validated best default:
                hybrid vector+keyword retrieval with rerank).
        """
        q = (query or "").strip()
        if not q:
            return "Error: query must not be empty"
        q = q[:_MAX_QUERY_CHARS]
        resolved_mode = mode if mode in _VALID_MODES else default_mode
        try:
            result = await client.query(query=q, workspace=workspace, mode=resolved_mode)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as a tool result, not raised
            logger.exception("brain_query failed")
            return f"Error: brain query failed ({exc})"

        if not result.response or not result.response.strip():
            return "The Brain has no relevant information for this query."

        text = result.response.strip()
        if result.references:
            sources = ", ".join(
                dict.fromkeys(r.file_path for r in result.references if r.file_path)
            )
            if sources:
                text += f"\n\n(Sources: {sources})"
        return text

    return FunctionTool.from_defaults(
        fn=brain_query,
        name=BRAIN_TOOL_NAME,
        description=(
            "Search the operator's Brain (uploaded documents, notes, and "
            "website content) and return a grounded, cited answer. Use for "
            "any question that might be covered by the organization's own "
            "product/service/policy documentation."
        ),
    )


def register_brain_tool(
    agent: _BrainToolAgent,
    client: _BrainQueryClient,
    *,
    workspace: str,
    instruction: str = "",
    mandatory: bool = False,
    default_mode: QueryMode = "mix",
) -> None:
    """Register ``brain_query`` on ``agent`` and inject the workflow prompt.

    This is the bot-agnostic seam: called once from ``cli/bot.py``
    ``_start()`` after ``bot.configure()``, never from a bot's own
    ``configure()``. The workflow instruction is *added* (not replacing any
    web-search workflow a bot may already have set — ``KaiAgent.add_tool_workflow``
    composes, see ``agent/core.py``).
    """
    tool = make_brain_query_tool(client, workspace=workspace, default_mode=default_mode)
    agent.register_tool(tool)
    agent.add_tool_workflow(build_brain_workflow_instruction(instruction, mandatory))
