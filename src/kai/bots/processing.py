"""Template-driven reply post-processing.

A :class:`PostProcessor` is built from a template's
:class:`~kai.templates.schema.PostProcessingConfig` and applied to an LLM reply
before delivery. Three profiles:

- ``default_cleanup`` — delegates to the monolithic
  :func:`kai.bots.text_cleanup.post_process` verbatim. Kept as one call
  rather than reimplemented as a step list: that function has ordering
  interdependencies (e.g. trailing-period detection depends on line-collapse
  having already run) that a naive step list would silently break.
- ``none`` — identity (email today: email supports markdown and has no
  post-processing).
- ``custom`` — runs only the individual flagged steps below, in a fixed order.

The custom step list is intentionally its own pipeline (not a teardown of
``post_process``): templates that opt into ``custom`` want explicit, predictable
transforms, not the full chat cleanup. The individual transform functions and
the emoji range are imported from :mod:`kai.bots.text_cleanup` so the two
pipelines never drift on what a given transform does.
"""

import re
from functools import partial

from kai.agent.core import strip_reasoning_channels
from kai.bots.text_cleanup import (
    collapse_lines,
    strip_emojis,
    strip_markdown,
    strip_trailing_period,
)
from kai.templates.schema import PostProcessingConfig

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
        if config.profile == "default_cleanup":
            self._fn = self._run_default_cleanup
        elif config.profile == "none":
            self._fn = lambda text: text  # noqa: E731
        else:  # "custom"
            steps: list = []
            if config.strip_markdown:
                steps.append(strip_markdown)
            if config.collapse_to_single_line:
                steps.append(collapse_lines)
            if config.strip_emojis:
                steps.append(strip_emojis)
            if config.strip_trailing_period:
                steps.append(strip_trailing_period)
            if config.max_sentences:
                steps.append(partial(_truncate_sentences, limit=config.max_sentences))
            if config.max_words:
                steps.append(partial(_truncate_words, limit=config.max_words))
            self._custom_steps = steps
            self._fn = self._run_custom

    @staticmethod
    def _run_default_cleanup(text: str) -> str:
        from kai.bots.text_cleanup import post_process

        return post_process(text)

    def _run_custom(self, text: str) -> str:
        # Strip leaked reasoning channels before custom profile transforms.
        text = strip_reasoning_channels(text)
        for step in self._custom_steps:
            text = step(text)
        return text.strip()

    def process(self, text: str) -> str:
        return self._fn(text)
