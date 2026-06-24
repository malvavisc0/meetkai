from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

from llama_index.core.tools import FunctionTool

from kai.agent.tools.calculator import calculate
from kai.agent.tools.hardware import (
    epaper_available,
    get_hardware_info,
    render_to_epaper,
)
from kai.agent.tools.time import get_current_datetime, get_weather
from kai.agent.tools.web import _get_webpage_content, _web_search

logger = logging.getLogger(__name__)

_LOG_REPR_LIMIT = 200


def _short_repr(value: Any, limit: int = _LOG_REPR_LIMIT) -> str:
    text = repr(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...(truncated, {len(text)} chars)"


def _logged(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        call_args = ", ".join(
            [_short_repr(a) for a in args] + [f"{k}={_short_repr(v)}" for k, v in kwargs.items()]
        )
        logger.info("tool call: %s(%s)", name, call_args)
        try:
            result = fn(*args, **kwargs)
        except Exception:
            logger.exception("tool %s raised an exception", name)
            raise
        logger.info("tool %s returned %s", name, _short_repr(result))
        return result

    return wrapper


def get_tools() -> list[FunctionTool]:
    specs: list[tuple[Callable[..., Any], str, str]] = [
        (
            _web_search,
            "web_search",
            "Search the web using DuckDuckGo. "
            "Returns a list of results with title, url, and snippet.",
        ),
        (
            _get_webpage_content,
            "get_webpage_content",
            "Fetch a webpage and convert its content to Markdown. "
            "Returns the page content, or a short 'Error: ...' string if "
            "the page cannot be fetched.",
        ),
        (
            get_current_datetime,
            "get_current_datetime",
            "Get the current date and time, optionally for a specific IANA "
            "timezone (e.g. Europe/Berlin). Use when someone asks the time "
            "in another region.",
        ),
        (
            get_weather,
            "get_weather",
            "Get current weather for a city, airport code, or lat,lon. "
            "Returns a concise summary. No API key needed.",
        ),
        (
            calculate,
            "calculate",
            "Safely evaluate a math expression (+ - * / // % **, parentheses, "
            "and functions like sqrt, abs, round, min, max). Use for any "
            "arithmetic or unit conversion.",
        ),
        (
            get_hardware_info,
            "get_hardware_info",
            "Get information about the host machine's hardware and OS: CPU "
            "architecture and count, memory and disk usage, and OS details. "
            "Use when asked about the server's specs or available resources.",
        ),
    ]

    if epaper_available():
        specs.append(
            (
                render_to_epaper,
                "render_to_epaper",
                "Render ASCII art to a Waveshare 2.13-inch e-Paper display "
                "(250x122 px, 1-bit black/white). Generates a bitmap from your "
                "ASCII art and pushes it to the physical screen. Falls back to "
                "saving a PNG file when hardware is unavailable. Art must fit "
                "~62 columns x 17 lines. Use monospace characters only. "
                "Generate art thematically inspired by the conversation.",
            )
        )
    return [
        FunctionTool.from_defaults(
            fn=_logged(name, fn),
            name=name,
            description=description,
        )
        for fn, name, description in specs
    ]


def get_tool_instructions(
    tools: list[FunctionTool], *, workflow_preamble: str | None = None
) -> str:
    """Render the tool section appended to the system prompt.

    Always emits a generic tool table. ``workflow_preamble`` is an optional
    block of tool-usage guidance a bot opts into (e.g. the web fact-checking
    workflow); ``None`` means a clean-slate bot that only wants the tool list
    with no chat-bot-specific workflow baked in.
    """
    if not tools:
        return ""

    rows = []
    for tool in tools:
        name = tool.metadata.name
        desc = tool.metadata.description or ""
        rows.append(f"| `{name}` | {desc} |")

    table = "\n".join(rows)
    out = (
        "\n\n# Tools\n"
        "\n"
        "You have access to the following tools. "
        "Use them when they genuinely help.\n"
        "\n"
        f"| Tool | Description |\n|------|-------------|\n{table}\n"
    )
    if workflow_preamble:
        out += "\n" + workflow_preamble
    return out


# Fact-checking workflow for chat bots that expose web search/fetch. Opt in via
# ``KaiAgent.set_tool_workflow(WEB_WORKFLOW_INSTRUCTIONS)``; non-chat bots pass
# ``None`` (the default) for a clean prompt with no web-search assumptions.
WEB_WORKFLOW_INSTRUCTIONS = (
    "**Workflow for answering factual questions:**\n"
    "1. `web_search` — find relevant results.\n"
    "2. `get_webpage_content` — visit the returned URLs and read their "
    "actual content. Single-source answers are how you get confidently "
    "wrong; cross-check across several independent pages.\n"
    "3. Synthesize your answer ONLY from page content you actually read.\n"
    "\n"
    "**Rules for tool use:**\n"
    "- ANYTHING time-sensitive or current — live scores, match status, "
    "weather, the time in another region, today's news, prices — MUST go "
    "through a tool. Your training data is frozen; you do not know these "
    "from memory. Answering real-time questions without a tool is guessing.\n"
    "- Never cite or mention a URL without visiting it first. "
    "A search snippet is not enough — always fetch the page.\n"
    "- When fact-checking a claim, visit at least 5 results with "
    "`get_webpage_content` before judging it true or false. Keep visiting "
    "results until you have read at least 5 pages with usable content, or "
    "you run out of results.\n"
    "- **Some sites block fetching and return an 'Error: HTTP 403/406' "
    "string or empty content. That is not a dead end:** move on to the "
    "NEXT search result and fetch it instead. Keep going down the result "
    "list until you have enough readable sources. Never stop and answer "
    "after one failed or empty fetch.\n"
    "- **Never state a specific number, score, statistic, or fact you did "
    "not read from a page you successfully fetched.** If after checking you "
    "could not read it from any source, say plainly that you couldn't "
    "verify it — do NOT guess, do NOT invent a plausible-sounding figure, "
    "and do NOT name a source you didn't actually open.\n"
    "- Don't search for stable facts you already know well (e.g. capital "
    "cities, basic math). But when in doubt whether something has changed, "
    "search — being current beats being fast.\n"
    "- Don't announce that you're searching — just do it and reply naturally.\n"
    "- If you say you'll check something, you MUST call a tool and deliver "
    "the result. Never promise to look something up and then go silent or guess.\n"
    "- If every result fails to fetch and you genuinely cannot verify, say "
    "so briefly in your own voice. Do not complain about the tool."
)
