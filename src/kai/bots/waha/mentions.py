from __future__ import annotations

import logging
import re
import unicodedata

from pydantic import BaseModel

from kai.bots.waha.jid import sanitize_display_name, user_digits

logger = logging.getLogger(__name__)

_MENTION_BRACKET_RE = re.compile(r"@\[([^\]\n]{1,80})\]")
# Inbound: WhatsApp/WAHA delivers user @-mentions inline in the body as a bare
# `@<digits>` token (the phone or LID prefix of the mentioned JID). We
# reverse-resolve these to `@[Name]` so the model sees a human name, not a
# numeric ID. Matches digit runs (>=5, to avoid clobbering e.g. "@2h")
# preceded by `@` and bounded by a non-word char or end of string.
_INBOUND_MENTION_RE = re.compile(r"@(\d{5,})(?=\W|$)")
_MENTION_BARE_RE = re.compile(
    r"@([\w\u00C0-\u024F\u0400-\u04FF\u0600-\u06FF\u0980-\u09FF"
    r"\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF"
    r"\u0370-\u03FF\u0590-\u05FF]{2,40})"
    r"(?=[^\w\u00C0-\u024F\u0400-\u04FF\u0600-\u06FF\u0980-\u09FF"
    r"\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF"
    r"\u0370-\u03FF\u0590-\u05FF]|$)",
    re.UNICODE,
)


class ResolvedReply(BaseModel):
    text: str
    mentions: list[str]


def _normalize(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    folded = normalized.casefold()
    result = []
    for ch in folded:
        cat = unicodedata.category(ch)
        if cat == "Mn":
            continue
        result.append(ch)
    return " ".join("".join(result).split())


def _build_normalized_index(
    roster: dict[str, str],
) -> tuple[dict[str, str], dict[str, str], set[str]]:
    full: dict[str, str] = {}
    first: dict[str, str] = {}
    ambiguous: set[str] = set()

    for jid, name in roster.items():
        normalized = _normalize(name)
        if not normalized:
            continue

        existing = full.get(normalized)
        if existing is not None and existing != jid:
            ambiguous.add(normalized)
            full.pop(normalized, None)
        elif existing is None:
            full[normalized] = jid

        words = normalized.split()
        first_word = words[0] if words else normalized
        if first_word:
            existing_first = first.get(first_word)
            if existing_first is not None and existing_first != jid:
                ambiguous.add(first_word)
                first.pop(first_word, None)
            elif existing_first is None:
                first[first_word] = jid

    return full, first, ambiguous


def _resolve_name(
    name: str,
    full_index: dict[str, str],
    first_index: dict[str, str],
    ambiguous: set[str],
    bot_digits: set[str],
) -> tuple[str, str | None]:
    key = _normalize(name)
    if not key:
        return "", None

    if key in ambiguous:
        logger.debug("Ambiguous mention (leaving as plain text): @[%s]", name)
        return name, None

    jid = full_index.get(key)
    if jid is None and len(key.split()) == 1:
        jid = first_index.get(key)
    if jid is None:
        logger.debug("Unresolved mention: @[%s]", name)
        return name, None

    digits = user_digits(jid)
    if digits in bot_digits:
        return name, None
    return f"@{digits}", jid


def strip_mention_markup(reply: str) -> str:
    """Remove ``@[Name]`` bracket markup, leaving the bare name.

    Used when outbound mentions are disabled (or in DMs) so the model's
    ``@[Name]`` tagging syntax never leaks literal brackets into the message.
    """
    return _MENTION_BRACKET_RE.sub(r"\1", reply or "")


def resolve_mentions(
    reply: str,
    roster: dict[str, str],
    bot_ids: set[str],
    is_group: bool,
) -> ResolvedReply:
    if not is_group:
        cleaned = _MENTION_BRACKET_RE.sub(r"\1", reply)
        cleaned = _MENTION_BARE_RE.sub(r"\1", cleaned)
        return ResolvedReply(text=cleaned, mentions=[])

    bot_digits = {user_digits(jid) for jid in bot_ids}
    full_index, first_index, ambiguous = _build_normalized_index(roster)

    seen: set[str] = set()
    mentions: list[str] = []

    def _add_mention(jid: str | None) -> None:
        if jid and jid not in seen:
            seen.add(jid)
            mentions.append(jid)

    def _replace_bracket(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        replacement, jid = _resolve_name(name, full_index, first_index, ambiguous, bot_digits)
        _add_mention(jid)
        return replacement

    def _replace_bare(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        replacement, jid = _resolve_name(name, full_index, first_index, ambiguous, bot_digits)
        if jid is None:
            return match.group(0)
        _add_mention(jid)
        return replacement

    text = _MENTION_BRACKET_RE.sub(_replace_bracket, reply)
    text = _MENTION_BARE_RE.sub(_replace_bare, text)
    return ResolvedReply(text=text, mentions=mentions)


def resolve_inbound_mentions(text: str, roster: dict[str, str], is_group: bool) -> str:
    """Rewrite inbound `@<digits>` mentions to `@[Name]` for the model.

    Symmetric with :func:`resolve_mentions` (which goes model→WhatsApp); this
    goes WhatsApp→model. WAHA delivers a user's @-mention inline in the body as
    a bare ``@<digits>`` token (the JID's phone/LID prefix). Without this, the
    model echoes the digits back (e.g. ``@123456789012345``) instead of a name.

    Only resolves digits that match a roster JID prefix; unmatched tokens
    (phone numbers that aren't participants, etc.) are left untouched. The
    sender's own name is seeded into the roster by the bot before this runs.
    """
    if not is_group or not text or not roster:
        return text

    digits_to_name: dict[str, str] = {}
    for jid, name in roster.items():
        digits = user_digits(jid)
        if digits and name:
            digits_to_name[digits] = name

    if not digits_to_name:
        return text

    def _replace(match: re.Match[str]) -> str:
        digits = match.group(1)
        name = digits_to_name.get(digits)
        if name is None:
            return match.group(0)
        return f"@[{sanitize_display_name(name)}]"

    return _INBOUND_MENTION_RE.sub(_replace, text)
