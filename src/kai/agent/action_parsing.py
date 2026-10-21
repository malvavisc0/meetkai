from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kai.agent.core import ActionResult

# Reasoning models sometimes emit their chain-of-thought wrapped in "channel"
# tokens (e.g. ``<|channel>thought ... <channel|>``). These must never reach
# the user: strip whole channel blocks first, then any stray channel markers.
REASONING_BLOCK_RE = re.compile(r"<\|?channel\|?>.*?<\|?channel\|?>", re.DOTALL)
REASONING_TOKEN_RE = re.compile(r"<\|?channel\|?>")


def strip_reasoning_channels(text: str) -> str:
    """Remove reasoning-model "channel" artifacts from model output.

    Strips ``<|channel>...<channel|>`` blocks and any leftover channel
    markers, returning the user-visible content. Returns the input unchanged
    (apart from stripping) if no channel tokens are present.
    """
    if not text:
        return text
    text = REASONING_BLOCK_RE.sub("", text)
    text = REASONING_TOKEN_RE.sub("", text)
    return text.strip()


KV_LINE_RE = re.compile(r'^[\s\-*]*["\']?(\w+)["\']?\s*[:=]\s*(.*)$')


def unescape_literal_newlines(text: str) -> str:
    """Convert literal ``\\n``/``\\r`` tokens into real line breaks.

    Some models serialize a multi-line reply as a single line with literal
    backslash-n tokens (a double-escaping of the JSON string value, or a
    ``key: value`` line that can't hold real newlines). Left alone the
    recipient sees visible ``\\n`` garbage instead of line breaks.

    This only fires when the text contains *no* real newline yet holds at
    least two literal ``\\n`` tokens — the signature of an escaped multi-line
    body. A reply that already has real newlines is left untouched (so a
    legit literal backslash-n is preserved), and a stray single ``\\n`` (e.g.
    a Windows path like ``C:\\new``) is not mangled.
    """
    if not text or "\n" in text:
        return text
    if text.count("\\n") < 2:
        return text
    return text.replace("\\r\\n", "\r\n").replace("\\n", "\n").replace("\\r", "\r")


def normalize_action_text(action: ActionResult) -> ActionResult:
    """Strip reasoning-channel artifacts and unescape literal newlines in text.

    Shared by the structured-output parser and the action-as-tool path so the
    two can't drift on what a cleaned ``text`` looks like before dispatch.
    Returns the same instance when there is nothing to normalize.
    """
    text = action.text
    if not text:
        return action
    cleaned = unescape_literal_newlines(strip_reasoning_channels(text))
    if cleaned == text:
        return action
    return action.model_copy(update={"text": cleaned})


def parse_kv_lines(text: str) -> dict[str, str]:
    """Parse ``key: value`` lines into a dict.

    Some models ignore the JSON instruction and emit the action as plain
    ``key: value`` lines (e.g. ``action: send_to_group\\ntext: hello``).
    This recovers those into a dict that ``model_validate`` can consume.
    """
    result: dict[str, str] = {}
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "```")):
            continue
        match = KV_LINE_RE.match(line)
        if not match:
            continue
        key = match.group(1).strip()
        value = match.group(2).strip().rstrip(",").strip("\"'`")
        if key:
            result[key] = value
    return result


def repair_json(text: str) -> str | None:
    """Attempt to extract and repair a JSON-like substring from text.

    Finds the first ``{``, then takes everything after it (to the end of
    the string or the last ``}``), and patches missing quotes and braces so
    partial JSON can still validate.
    """
    left = text.find("{")
    if left == -1:
        return None
    right = text.rfind("}")
    candidate = text[left : right + 1] if right > left else text[left:]
    quote_count = candidate.count('"')
    if quote_count % 2 == 1:
        candidate += '"'
    brace_count = candidate.count("{") - candidate.count("}")
    if brace_count > 0:
        candidate += "}" * brace_count
    return candidate


def action_values(output_cls: type[ActionResult]) -> frozenset[str]:
    """Extract the allowed ``action`` enum values from a bot's ``output_cls``."""
    try:
        schema = output_cls.model_json_schema()
        action_prop = (schema.get("properties") or {}).get("action", {})
        enum = action_prop.get("enum")
        if enum:
            return frozenset(enum)
    except Exception:
        pass
    return frozenset()
