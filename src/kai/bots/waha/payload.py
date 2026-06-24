from __future__ import annotations

import logging
from dataclasses import dataclass

from kai.bots.waha.media import MediaType, extract_media

logger = logging.getLogger(__name__)

GROUP_SUFFIX = "@g.us"


@dataclass(frozen=True)
class MessageMetadata:
    sender_name: str
    sender_id: str
    chat_id: str
    is_group: bool
    mentions_bot: bool = False
    has_media: bool = False
    media_type: MediaType = MediaType.UNKNOWN


def _extract_sender_id(msg: dict) -> str:
    participant = msg.get("participant")
    if participant:
        return participant
    author = msg.get("_data", {}).get("author")
    if isinstance(author, dict):
        return author.get("_serialized", "")
    if isinstance(author, str) and author:
        return author
    return msg.get("from", "")


def _extract_sender_name(msg: dict, sender_id: str) -> str:
    data = msg.get("_data", {})
    name = data.get("notifyName") or msg.get("notifyName") or ""
    name = name.strip()
    if name:
        return _sanitize_name(name)
    return sender_id.split("@")[0] if sender_id else "unknown"


def _sanitize_name(name: str) -> str:
    return name.replace("[", "").replace("]", "").replace("\n", " ").strip()[:80]


def _extract_mentioned_jids(msg: dict) -> list[str]:
    data = msg.get("_data", {})
    jids: list[str] = []
    for key in ("mentionedJidList", "groupMentions"):
        for entry in data.get(key, []):
            if isinstance(entry, str) and entry:
                jids.append(entry)
            elif isinstance(entry, dict):
                serialized = entry.get("_serialized", "")
                if serialized:
                    jids.append(serialized)
    return jids


def _user_digits(jid: str) -> str:
    return jid.split("@")[0]


def _jid_matches(jid: str, bot_ids: set[str]) -> bool:
    """Return True if ``jid`` refers to one of the bot's own identities.

    Matching is intentionally loose on the JID suffix: WAHA exposes the bot
    under both ``@c.us`` (phone) and ``@lid`` (privacy-preserving LID) forms,
    and a single account can surface under either. We compare the bare digit
    prefix so a mention/reply in either namespace is recognized when the
    identifiers share that prefix.

    Limitation (audit C2): a true LID↔c.us mapping cannot be derived from the
    digit prefix alone — LIDs are opaque and unrelated to the phone number.
    Resolving a ``@c.us`` bot against an unrelated ``@lid`` mention requires
    WAHA's contact/identity resolution endpoints, which are not wired here.
    """
    if jid in bot_ids:
        return True
    jid_digits = _user_digits(jid)
    return any(_user_digits(bid) == jid_digits for bid in bot_ids)


def parse_message(payload: dict, bot_ids: set[str] | None = None) -> MessageMetadata:
    msg = payload.get("payload", payload)

    chat_id = msg.get("from", "")
    is_group = GROUP_SUFFIX in chat_id
    sender_id = _extract_sender_id(msg)
    sender_name = _extract_sender_name(msg, sender_id)

    mentions_bot = False
    if bot_ids:
        mentioned_jids = _extract_mentioned_jids(msg)
        for jid in mentioned_jids:
            if _jid_matches(jid, bot_ids):
                mentions_bot = True
                break

    media = extract_media(msg)
    has_media = media is not None
    media_type = media.type if media else MediaType.UNKNOWN

    return MessageMetadata(
        sender_name=sender_name,
        sender_id=sender_id,
        chat_id=chat_id,
        is_group=is_group,
        mentions_bot=mentions_bot,
        has_media=has_media,
        media_type=media_type,
    )
