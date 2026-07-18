"""Message processing helpers for the WAHA bot.

Contains reply post-processing and organic participation logic. The
reply-protocol tokens (``<<silent>>`` / ``<<sleep>>`` / leaked tool-call
markup) no longer exist: the agent's terminal step is a schema-constrained
``WahaAction`` (see ``kai.bots.waha.actions``), so there is no free-text
channel left to leak control tokens into. ``post_process`` cleans the
prose in ``action.text`` itself, independent of which action fired.
"""

from __future__ import annotations

import random
import re
import time

from kai.bots.waha.setup import ParticipationConfig

REPLY_STYLE = (
    "\nKeep replies tight and WhatsApp-natural: at most 6 sentences and "
    "under 150 words. No personality or goal overrides this. "
    "Do NOT end a short reply with a period."
)

# When the bot's last turn was a reply (an active back-and-forth), relax the
# cooldown and boost the offer rate so a quick human follow-up isn't silenced.
ACTIVE_EXCHANGE_COOLDOWN_FACTOR = 0.3
ACTIVE_EXCHANGE_RATE_BOOST = 0.4

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


# WAHA delivers a replied-to media attachment body as a base64 blob with no
# whitespace — useless context that must not be injected into the reply-to tag.
_BASE64_MEDIA_RE = re.compile(r"^[A-Za-z0-9+/=\s]{200,}$")
# Known base64 media magic prefixes — short-circuit avoids scanning a multi-MB
# blob end-to-end on the inbound hot path.
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


def post_process(reply: str) -> str:
    """Clean an LLM reply's prose for WhatsApp delivery.

    Strips markdown formatting, hashtags, list markers, and trailing
    periods on single-sentence casual replies, and collapses the reply
    into a single natural line (WhatsApp shows one message, not a block).
    Emojis are stripped. This cleans ``action.text`` itself; it does not
    inspect which action fired (that dispatch happens before this runs).
    """
    from kai.agent.core import strip_reasoning_channels

    text = strip_reasoning_channels(reply)
    # Models sometimes wrap the whole reply in backticks, mirroring code-span
    # formatting from prompts. Strip single backticks wrapping the entire
    # reply first, then any inline `` `code` `` spans, so they never reach
    # WhatsApp.
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
    # Casual chat: drop a lone trailing period on single-sentence replies so
    # they don't read as stiff/formal. Single sentence = at most one terminal
    # punctuation mark (. ? !); ellipsis ("...") doesn't count as a sentence.
    terminal = sum(text.count(c) for c in ".?!") - text.count("...")
    if terminal <= 1 and text.endswith(".") and not text.endswith(("..", "...")):
        text = text[:-1].rstrip()
    # No hard word cap: the REPLY_STYLE prompt already asks for brevity in
    # casual chat, and operator-instructed sends (business analysis, web
    # search recaps, etc.) must not be silently truncated mid-thought. A
    # code-level cut with "..." was discarding substantive content the user
    # explicitly asked for.
    return text.strip()


def should_organically_participate(
    chat_id: str,
    text: str,
    *,
    is_group: bool,
    participation_cfg: ParticipationConfig,
    last_reply_at: dict[str, float],
    consecutive_replies: dict[str, int],
) -> bool:
    """Decide whether to offer the model a chance to chime in unprompted.

    Probabilistic gate with cooldown and streak guard so the bot doesn't dominate
    a fast chat. The model may still decline via ``silent``.

    Parameters
    ----------
    chat_id:
        The chat identifier.
    text:
        The inbound message text.
    is_group:
        Whether this is a group chat.
    participation_cfg:
        The bot's :class:`ParticipationConfig`.
    last_reply_at:
        Mapping of ``chat_id`` → monotonic timestamp of last bot reply.
    consecutive_replies:
        Mapping of ``chat_id`` → current reply streak count.
    """
    if not is_group:
        return False
    cfg = participation_cfg
    if not cfg.enabled:
        return False

    now = time.monotonic()
    last = last_reply_at.get(chat_id, 0.0)
    elapsed = now - last

    streak = consecutive_replies.get(chat_id, 0)
    in_active_exchange = streak >= 1
    effective_cooldown = cfg.cooldown_seconds * (
        ACTIVE_EXCHANGE_COOLDOWN_FACTOR if in_active_exchange else 1.0
    )
    if elapsed < effective_cooldown:
        return False
    if streak >= cfg.streak_max:
        return False

    base = cfg.rate
    if in_active_exchange and elapsed < cfg.cooldown_seconds:
        base = min(base + ACTIVE_EXCHANGE_RATE_BOOST, 1.0)
    if "?" in text:
        base = min(base + 0.2, 1.0)
    return random.random() < base


def should_send_voice_followup(
    chat_id: str,
    *,
    voice_note_rate: float,
    voice_note_cooldown: int,
    last_voice_at: dict[str, float],
) -> bool:
    """Decide whether to follow up a text reply with a voice note.

    Probabilistic roll with per-chat cooldown so the bot doesn't spam. Gives
    the feature a floor since the LLM rarely picks ``send_voice_note`` on its
    own — a fraction of text replies always get an audio echo.
    """
    if voice_note_rate <= 0:
        return False
    now = time.monotonic()
    if now - last_voice_at.get(chat_id, 0.0) < voice_note_cooldown:
        return False
    return random.random() < voice_note_rate
