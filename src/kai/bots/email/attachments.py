"""Attachment download and tagging for inbound email events."""

import logging
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


async def download_attachment(url: str, max_bytes: int) -> bytes | None:
    """Download an attachment with a size cap.

    The URL comes from Resend's signed webhook (already verified by the
    cockpit's ingress route), so it's trusted as a source — but the content
    is still untrusted user data (prompt-injection guard in ``prompt.md``
    covers this).
    """
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        logger.warning("Attachment too large: over %d bytes", max_bytes)
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
    except Exception as exc:
        host = urlparse(url).hostname or "unknown host"
        logger.warning("Attachment download failed from %s: %s", host, type(exc).__name__)
        return None


async def process_attachments(
    text: str,
    attachments: list[dict],
    *,
    vision_enabled: bool,
    max_attachment_bytes: int,
) -> tuple[list[bytes], str]:
    """Download image attachments, tag non-image attachments.

    Returns ``(image_bytes_list, enriched_text)``. Image bytes are only
    downloaded when vision is enabled; otherwise (or for non-images) a
    ``[attachment: ...]`` text tag is injected — graceful degradation when
    ``image_enabled=False``.
    """
    images: list[bytes] = []
    enriched = text

    for att in attachments:
        content_type = att.get("content_type", "")
        filename = att.get("filename", "")
        url = att.get("url", "")

        if content_type.startswith("image/") and vision_enabled:
            img_bytes = await download_attachment(url, max_attachment_bytes)
            if img_bytes:
                images.append(img_bytes)
                tag = f"[image attached: {filename}]"
            else:
                tag = f"[image attachment failed to download: {filename}]"
        else:
            tag = f"[attachment: {filename} ({content_type})]"

        enriched = f"{tag}\n{enriched}" if enriched else tag

    return images, enriched
