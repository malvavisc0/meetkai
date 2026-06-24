from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)


class MediaType(StrEnum):
    IMAGE = "image"
    VOICE = "voice"
    AUDIO = "audio"
    UNKNOWN = "unknown"


_TYPE_MAP: dict[str, MediaType] = {
    "image": MediaType.IMAGE,
    "ptt": MediaType.VOICE,
    "audio": MediaType.AUDIO,
}


@dataclass(frozen=True)
class MediaAttachment:
    type: MediaType
    mime_type: str
    url: str | None
    data: bytes | None
    filename: str


def extract_media(msg: dict) -> MediaAttachment | None:
    msg_type = msg.get("type", "")
    media_type = _TYPE_MAP.get(msg_type, MediaType.UNKNOWN)

    if media_type is MediaType.UNKNOWN:
        return None

    mime_type = msg.get("mimetype", "")
    filename = msg.get("filename", "")

    data_field = msg.get("data")
    media_url = msg.get("mediaUrl")

    # WAHA's REST API (and sometimes webhooks) deliver media as a nested
    # ``media`` dict with a ``url`` key pointing at /api/files/...  This form
    # appears when a message is fetched with downloadMedia=true.
    media_obj = msg.get("media")
    if isinstance(media_obj, dict) and media_obj.get("url") and isinstance(media_obj["url"], str):
        media_url = media_obj["url"]
        if not mime_type and media_obj.get("mimetype"):
            mime_type = media_obj["mimetype"]
        if not filename and media_obj.get("filename"):
            filename = media_obj["filename"]

    if data_field and isinstance(data_field, str):
        try:
            decoded = base64.b64decode(data_field)
        except Exception:
            logger.warning("Failed to decode inline base64 media (type=%s)", msg_type)
            return None
        return MediaAttachment(
            type=media_type,
            mime_type=mime_type,
            url=None,
            data=decoded,
            filename=filename,
        )

    if media_url and isinstance(media_url, str):
        return MediaAttachment(
            type=media_type,
            mime_type=mime_type,
            url=media_url,
            data=None,
            filename=filename,
        )

    return None
