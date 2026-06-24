"""Message processing helpers for the WAHA bot.

Contains reply post-processing, sleep-mode detection, organic participation
logic, and the related constants/regexes.  Extracted from ``__init__.py``
to keep the main bot module focused on orchestration.
"""

from __future__ import annotations

import random
import re
import time

REPLY_STYLE = (
    "\nCRITICAL: Your reply MUST be 1-2 sentences max, under 40 words. "
    "No exceptions. No personality or goal overrides this limit. "
    "Do NOT end with an emoji — most replies have zero emoji. "
    "Do NOT end a short reply with a period."
)

# When the bot's last turn was a reply (an active back-and-forth), relax the
# cooldown and boost the offer rate so a quick human follow-up isn't silenced.
ACTIVE_EXCHANGE_COOLDOWN_FACTOR = 0.3
ACTIVE_EXCHANGE_RATE_BOOST = 0.4

# The sleep state is driven entirely by the model, not regex. When the model
# decides the chat wants it to go quiet (in ANY language or dialect), it emits
# the <<sleep>> token — exactly like <<silent>>. The system then mutes that
# chat until the model, given a chance on a direct mention/reply, replies with
# real content (which clears the sleep state).
SLEEP_MARKER = "<<sleep>>"
_SLEEP_RE = re.compile(re.escape(SLEEP_MARKER), re.IGNORECASE)
DEFAULT_SLEEP_ACK = "going quiet, ping me if you need me"

_EMOJI_BASE = (
    "\U0001f600-\U0001f64f\U0001f300-\U0001f5ff\U0001f680-\U0001f6ff"
    "\U0001f1e0-\U0001f1ff\U00002702-\U000027b0\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001fa6f\U0001fa70-\U0001faff\U00002600-\U000026ff"
)

_EMOJI_CLUSTER_RE = re.compile(
    rf"[{_EMOJI_BASE}][\U0000fe00-\U0000fe0f]?"
    rf"(?:\u200d[{_EMOJI_BASE}][\U0000fe00-\U0000fe0f]?)*",
    flags=re.UNICODE,
)


def has_sleep_token(reply: str) -> bool:
    return bool(_SLEEP_RE.search(reply or ""))


def strip_sleep_token(reply: str) -> str:
    return _SLEEP_RE.sub("", reply or "").strip()


def post_process(reply: str) -> str:
    """Clean an LLM reply for WhatsApp delivery.

    Strips markdown formatting, excess emojis, hashtags, and trailing
    periods on short casual replies.
    """
    from kai.agent.core import strip_reasoning_channels

    text = strip_reasoning_channels(reply)
    # Models sometimes wrap the whole reply (or a `<<silent>>`/`<<sleep>>`
    # token) in backticks, mirroring code-span formatting from prompts.
    # Strip single backticks wrapping the entire reply first, then any
    # inline `` `code` `` spans, so they never reach WhatsApp.
    text = re.sub(r"^`+\s*", "", text)
    text = re.sub(r"\s*`+$", "", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"#\w+", "", text)
    clusters = _EMOJI_CLUSTER_RE.findall(text)
    if len(clusters) > 1:
        first = clusters[0]
        cleaned = _EMOJI_CLUSTER_RE.sub("", text).strip()
        text = f"{cleaned} {first}".strip() if first else cleaned
    # Casual chat: drop a single trailing period on short replies so they
    # don't read as stiff/formal. Preserves "?", "!", "..." and multi-sentence
    # replies that need internal punctuation.
    if len(text) <= 60 and text.endswith(".") and not text.endswith(("..", "...")):
        text = text[:-1].rstrip()
    return text.strip()


def should_organically_participate(
    chat_id: str,
    text: str,
    *,
    is_group: bool,
    participation_cfg: object,
    last_reply_at: dict[str, float],
    consecutive_replies: dict[str, int],
) -> bool:
    """Decide whether to offer the model a chance to chime in unprompted.

    Probabilistic + cooldown + streak guard so the bot doesn't dominate or
    machine-gun a fast chat. The model may still decline via ``<<silent>>``.

    Parameters
    ----------
    chat_id:
        The chat identifier.
    text:
        The inbound message text.
    is_group:
        Whether this is a group chat.
    participation_cfg:
        A :class:`ParticipationConfig` instance (duck-typed: needs
        ``enabled``, ``rate``, ``cooldown_seconds``, ``streak_max``).
    last_reply_at:
        Mapping of ``chat_id`` → monotonic timestamp of last bot reply.
    consecutive_replies:
        Mapping of ``chat_id`` → current reply streak count.
    """
    if not is_group:
        return False
    cfg = participation_cfg
    if not cfg.enabled:  # type: ignore[union-attr]
        return False

    now = time.monotonic()
    last = last_reply_at.get(chat_id, 0.0)
    elapsed = now - last

    streak = consecutive_replies.get(chat_id, 0)
    in_active_exchange = streak >= 1
    effective_cooldown = cfg.cooldown_seconds * (  # type: ignore[union-attr]
        ACTIVE_EXCHANGE_COOLDOWN_FACTOR if in_active_exchange else 1.0
    )
    if elapsed < effective_cooldown:
        return False
    if streak >= cfg.streak_max:  # type: ignore[union-attr]
        return False

    base = cfg.rate  # type: ignore[union-attr]
    if in_active_exchange and elapsed < cfg.cooldown_seconds:  # type: ignore[union-attr]
        base = min(base + ACTIVE_EXCHANGE_RATE_BOOST, 1.0)
    if "?" in text:
        base = min(base + 0.2, 1.0)
    return random.random() < base
