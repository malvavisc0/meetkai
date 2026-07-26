"""Transport-neutral reply text cleanup.

Provides the monolithic :func:`post_process` pipeline and the individual
transform steps it's built from, plus the shared ``_EMOJI_RE`` emoji range.

The individual steps (``strip_markdown``, ``collapse_lines``, ``strip_emojis``,
``strip_trailing_period``) are the single source of truth — both
:func:`post_process` and ``kai.bots.processing``'s custom-profile step list
import them, so the two never drift on what a given transform does.
``post_process`` keeps its original ordering (markdown strip → line collapse →
emoji strip → trailing-period guard) because each step depends on the previous
one having run.
"""

import re

from kai.agent.core import strip_reasoning_channels

# Emoji over-use is the #1 way the bot reads as a bot. Covers pictographs,
# dingbats, flags (regional indicators), and the modern supplementary emoji
# blocks. Shared by ``post_process`` and the custom ``PostProcessor`` step list
# so the two never drift on what counts as an emoji.
_EMOJI_RE = re.compile(
    "["
    "\U0001f600-\U0001f64f"
    "\U0001f300-\U0001f5ff"
    "\U0001f680-\U0001f6ff"
    "\U0001f700-\U0001f77f"
    "\U0001f780-\U0001f7ff"
    "\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001fa6f"
    "\U0001fa70-\U0001faff"
    "\U0001f1e0-\U0001f1ff"
    "\U00002600-\U000026ff"
    "\U00002700-\U000027bf"
    "\U00002b00-\U00002bff"
    "\U0000fe0f"
    "\u200d"
    "]",
    flags=re.UNICODE,
)


def strip_markdown(text: str) -> str:
    """Strip markdown formatting to plain text.

    Inline code spans (`` `code` ``) and wrapping backticks, links → label
    only, bold/italic/underscore, list markers (bullets and numbered), and
    hashtags.
    """
    # Models sometimes wrap the whole reply in backticks, mirroring code-span
    # formatting from prompts. Strip single backticks wrapping the entire
    # reply first, then any inline `` `code` `` spans, so they never reach
    # the chat.
    text = re.sub(r"^`+\s*", "", text)
    text = re.sub(r"\s*`+$", "", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    # Markdown inline formatting → plain text.
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    # List markers (bullets and numbered).
    text = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"#\w+", "", text)
    return text


def collapse_lines(text: str) -> str:
    """Collapse newlines (and surrounding whitespace) into single spaces."""
    return re.sub(r"\s*\n\s*", " ", text)


def strip_emojis(text: str) -> str:
    """Replace every emoji with a space, then collapse doubled whitespace."""
    text = _EMOJI_RE.sub(" ", text)
    return re.sub(r"\s{2,}", " ", text)


def strip_trailing_period(text: str) -> str:
    """Drop a lone trailing period on single-sentence casual replies.

    Single sentence = at most one terminal punctuation mark (. ? !); ellipsis
    ("...") doesn't count as a sentence.
    """
    terminal = sum(text.count(c) for c in ".?!") - text.count("...")
    if terminal <= 1 and text.endswith(".") and not text.endswith(("..", "...")):
        return text[:-1].rstrip()
    return text


def post_process(reply: str) -> str:
    """Clean an LLM reply's prose for chat delivery.

    Strips markdown formatting, hashtags, list markers, and trailing
    periods on single-sentence casual replies, and collapses the reply
    into a single natural line (a chat shows one message, not a block).
    Emojis are stripped.

    Composes the individual transforms above in the fixed order they must
    run in (each step depends on the previous one having run).
    """
    text = strip_reasoning_channels(reply)
    text = strip_markdown(text)
    text = collapse_lines(text)
    # Strip every emoji — the prompt's default is no emoji, and small models
    # over-use them. Done after line-collapse so emoji-only spacing doesn't
    # leave doubled spaces.
    text = strip_emojis(text)
    text = strip_trailing_period(text)
    return text.strip()
