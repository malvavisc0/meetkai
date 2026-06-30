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
    "\nCRITICAL: Your reply MUST be at most 3 sentences and under 60 words. "
    "No exceptions. No personality or goal overrides this limit. "
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
SILENT_MARKER = "<<silent>>"
_SILENT_TOKEN_RE = re.compile(re.escape(SILENT_MARKER), re.IGNORECASE)
DEFAULT_SLEEP_ACK = "going quiet, ping me if you need me"


# Hallucinated/leaked tool calls occasionally arrive as plain assistant text
# (the model emits a tool-call block the runtime doesn't parse, so it falls
# through as content). These artifacts must never be delivered to the chat.
_TOOL_CALL_LEAK_RE = re.compile(
    r"</?tool_call>|</?arg_key>|</?arg_value>|<arg(?:_)?key>|<arg(?:_)?value>",
    re.IGNORECASE,
)

# Emoji over-use is the #1 way the bot reads as a bot. The prompt already
# mandates "default to NO emoji", but small models (gemma) ignore that and
# stack emojis on almost every reply. Strip them all post-hoc so a reply is
# always plain prose — matching the documented "a plain reply is always
# better" default. Covers pictographs, dingbats, flags (regional indicators),
# and the modern supplementary emoji blocks.
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


# A replied-to message whose body is a media attachment is delivered by WAHA
# as a long base64 blob (JPEG `/9j/...`, PNG `iVBOR...`, WebP, audio, …) with no
# whitespace. Such a blob is useless context for the model and bloats the turn,
# so it must never be injected into the reply-to tag. A normal text reply is
# short and almost always contains spaces/punctuation outside the base64 charset.
_BASE64_MEDIA_RE = re.compile(r"^[A-Za-z0-9+/=\s]{200,}$")
# Known base64 media magic prefixes (data-url / raw blob). Short-circuiting on
# these avoids scanning a multi-MB blob end-to-end on the inbound hot path.
_MEDIA_B64_PREFIXES = (
    "/9j/",
    "iVBOR",
    "UklGR",
    "RIFF",
    "AAAA",
    "GkX",  # JPEG/PNG/WebP/OGG/AVI/WebM
)


def looks_like_base64_media(body: str) -> bool:
    """Return True if ``body`` looks like an embedded media base64 blob.

    Matches long runs of base64 characters (optionally with whitespace) with no
    normal prose. A genuine text reply of this length is effectively impossible
    to be pure base64, so this is a safe filter for reply-to bodies.

    Only the first 4 KiB is scanned: a base64 blob is base64 from the very first
    character, so a prefix check is sufficient and avoids running an anchored
    regex over a multi-MB blob on the inbound hot path.
    """
    if not body or len(body) < 200:
        return False
    if body.startswith(_MEDIA_B64_PREFIXES):
        return True
    return bool(_BASE64_MEDIA_RE.match(body[:4096]))


def has_tool_call_leak(reply: str) -> bool:
    """Return True if ``reply`` contains leaked tool-call markup.

    Detects the structural artifacts of an unparsed tool call (``<tool_call>``,
    ``<arg_key>``/``<arg_value>`` and their hyphen-less variants). These should
    never reach WhatsApp, so the caller treats such a reply as silent.
    """
    return bool(_TOOL_CALL_LEAK_RE.search(reply or ""))


def has_sleep_token(reply: str) -> bool:
    return bool(_SLEEP_RE.search(reply or ""))


def strip_sleep_token(reply: str) -> str:
    return _SLEEP_RE.sub("", reply or "").strip()


def strip_silent_token(reply: str) -> str:
    """Remove any ``<<silent>>`` token (case-insensitive) from ``reply``.

    Unlike :func:`has_silent_reply`-style anchored detection, this strips a
    stray token embedded in real content so the surrounding text still ships.
    Built from :data:`SILENT_MARKER` so it tracks the constant.
    """
    return _SILENT_TOKEN_RE.sub("", reply or "").strip()


def post_process(reply: str) -> str:
    """Clean an LLM reply for WhatsApp delivery.

    Strips markdown formatting, hashtags, list markers, and trailing
    periods on single-sentence casual replies, collapses the reply into a
    single natural line (WhatsApp shows one message, not a block), and
    defensively removes any stray ``<<silent>>``/``<<sleep>>`` tokens. Emojis
    are preserved exactly as the model produced them.
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
    # Markdown inline formatting → plain text.
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    # List markers (bullets and numbered) — a WhatsApp message is prose, not a
    # list. Strip the leading marker on each line before collapsing newlines.
    text = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"#\w+", "", text)
    # Collapse to one natural line: the persona is a single short WhatsApp
    # message, never a multi-paragraph block.
    text = re.sub(r"\s*\n\s*", " ", text)
    # Strip every emoji — the prompt's default is no emoji, and small models
    # over-use them. Done after line-collapse so emoji-only spacing doesn't
    # leave doubled spaces. Also drop the variation selector (FE0F) and tidy
    # any whitespace the removals opened up.
    text = _EMOJI_RE.sub(" ", text)
    text = re.sub(r"\s{2,}", " ", text)
    # Defensive: a stray control token must never ship to WhatsApp even if the
    # caller's silence/sleep detection somehow missed it. Built from the shared
    # SILENT_MARKER/SLEEP_MARKER constants so the patterns can't drift.
    text = strip_silent_token(text)
    text = strip_sleep_token(text)
    # Casual chat: drop a lone trailing period on single-sentence replies so
    # they don't read as stiff/formal. Single sentence = at most one terminal
    # punctuation mark (. ? !); ellipsis ("...") doesn't count as a sentence.
    terminal = sum(text.count(c) for c in ".?!") - text.count("...")
    if terminal <= 1 and text.endswith(".") and not text.endswith(("..", "...")):
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
