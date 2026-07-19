"""Template-driven reply post-processing.

A :class:`PostProcessor` is built from a template's
:class:`~kai.templates.schema.PostProcessingConfig` and applied to an LLM reply
before delivery. Three profiles:

- ``waha_default`` — delegates to the existing monolithic
  :func:`kai.bots.waha.processing.post_process` verbatim. Kept as one call
  rather than reimplemented as a step list: that function has ordering
  interdependencies (e.g. trailing-period detection depends on line-collapse
  having already run) that a naive step list would silently break.
- ``none`` — identity (email today: email supports markdown and has no
  post-processing).
- ``custom`` — runs only the individual flagged steps below, in a fixed order.

The custom step list is intentionally its own pipeline (not a teardown of
``post_process``): templates that opt into ``custom`` want explicit, predictable
transforms, not the waha persona's full cleanup. The emoji range is shared with
``post_process`` (via ``_EMOJI_RE``) so the two never drift on what counts as
an emoji.
"""

import re
from functools import partial

from kai.agent.core import strip_reasoning_channels
from kai.templates.schema import PostProcessingConfig


def _strip_markdown(text: str) -> str:
    # Inline code spans (`` `code` ``) and wrapping backticks.
    text = re.sub(r"^`+\s*", "", text)
    text = re.sub(r"\s*`+$", "", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    # Links → label only.
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    # List markers (bullets and numbered).
    text = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"#\w+", "", text)
    return text


def _collapse_lines(text: str) -> str:
    return re.sub(r"\s*\n\s*", " ", text)


def _strip_emojis(text: str) -> str:
    from kai.bots.waha.processing import _EMOJI_RE

    text = _EMOJI_RE.sub(" ", text)
    return re.sub(r"\s{2,}", " ", text)


def _strip_trailing_period(text: str) -> str:
    terminal = sum(text.count(c) for c in ".?!") - text.count("...")
    if terminal <= 1 and text.endswith(".") and not text.endswith(("..", "...")):
        return text[:-1].rstrip()
    return text


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _truncate_sentences(text: str, *, limit: int) -> str:
    sentences = _SENTENCE_SPLIT_RE.split(text.strip())
    if len(sentences) <= limit:
        return text.strip()
    return " ".join(sentences[:limit]).strip()


_WORD_SPLIT_RE = re.compile(r"\s+")


def _truncate_words(text: str, *, limit: int) -> str:
    words = _WORD_SPLIT_RE.split(text.strip())
    if len(words) <= limit:
        return text.strip()
    return " ".join(words[:limit]).strip()


class PostProcessor:
    """Apply a template's post-processing profile to reply text."""

    def __init__(self, config: PostProcessingConfig) -> None:
        self._config = config
        if config.profile == "waha_default":
            self._fn = self._run_waha_default
        elif config.profile == "none":
            self._fn = lambda text: text  # noqa: E731
        else:  # "custom"
            steps: list = []
            if config.strip_markdown:
                steps.append(_strip_markdown)
            if config.collapse_to_single_line:
                steps.append(_collapse_lines)
            if config.strip_emojis:
                steps.append(_strip_emojis)
            if config.strip_trailing_period:
                steps.append(_strip_trailing_period)
            if config.max_sentences:
                steps.append(partial(_truncate_sentences, limit=config.max_sentences))
            if config.max_words:
                steps.append(partial(_truncate_words, limit=config.max_words))
            self._custom_steps = steps
            self._fn = self._run_custom

    @staticmethod
    def _run_waha_default(text: str) -> str:
        from kai.bots.waha.processing import post_process

        return post_process(text)

    def _run_custom(self, text: str) -> str:
        # Strip leaked reasoning channels before custom profile transforms.
        text = strip_reasoning_channels(text)
        for step in self._custom_steps:
            text = step(text)
        return text.strip()

    def process(self, text: str) -> str:
        return self._fn(text)
