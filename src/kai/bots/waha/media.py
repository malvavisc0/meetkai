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


def _infer_media_type(msg: dict, mime_type: str) -> MediaType:
    """Resolve the :class:`MediaType` for a WAHA message.

    The top-level ``type`` field is frequently ``None`` in WAHA REST/webhook
    payloads (the API spec marks it optional and the runtime often omits it).
    The real type lives in ``_data.type`` (e.g. ``image``/``ptt``/``audio``)
    or is derivable from the nested ``media.mimetype``. We check all three
    sources so media is never silently dropped just because ``type`` was null.
    """
    msg_type = msg.get("type") or ""
    if msg_type:
        mapped = _TYPE_MAP.get(msg_type)
        if mapped:
            return mapped

    data = msg.get("_data")
    if isinstance(data, dict):
        data_type = data.get("type") or ""
        if data_type:
            mapped = _TYPE_MAP.get(data_type)
            if mapped:
                return mapped

    if mime_type:
        if mime_type.startswith("image/"):
            return MediaType.IMAGE
        if mime_type.startswith("audio/"):
            # ptt voice notes and regular audio are both transcribable; the
            # handler treats VOICE/AUDIO identically, so map all audio here.
            return MediaType.VOICE
    return MediaType.UNKNOWN


def extract_media(msg: dict) -> MediaAttachment | None:
    mime_type = msg.get("mimetype") or ""
    filename = msg.get("filename") or ""

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
    else:
        media_url = msg.get("mediaUrl")

    media_type = _infer_media_type(msg, mime_type)
    if media_type is MediaType.UNKNOWN:
        return None

    data_field = msg.get("data")
    if data_field and isinstance(data_field, str):
        try:
            decoded = base64.b64decode(data_field)
        except Exception:
            logger.warning("Failed to decode inline base64 media (type=%s)", media_type)
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
