"""Attachment download and tagging for inbound email events."""

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

MAX_REDIRECTS = 3
DNS_TIMEOUT = 5.0


async def host_is_public(host: str) -> bool:
    """Return True when every resolved address for ``host`` is globally routable."""
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not address.is_global:
            return False
    return True


async def validate_fetch_url(url: str, cache: dict[str, bool] | None = None) -> bool:
    """Return True when the URL is HTTPS and resolves only to public addresses.

    ``cache`` avoids repeating DNS lookups for the same host within one
    attachment download (including redirects).
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        logger.warning("Attachment URL rejected: unsupported scheme or host")
        return False
    host = parsed.hostname
    if cache is not None and host in cache:
        allowed = cache[host]
    else:
        try:
            allowed = await asyncio.wait_for(host_is_public(host), timeout=DNS_TIMEOUT)
        except TimeoutError:
            allowed = False
        if cache is not None:
            cache[host] = allowed
    if not allowed:
        logger.warning("Attachment host rejected: %s", host)
        return False
    return True


async def read_capped_body(resp: httpx.Response, max_bytes: int) -> bytes | None:
    """Read a streamed body, returning None once it exceeds ``max_bytes``."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            logger.warning("Attachment too large: over %d bytes", max_bytes)
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def download_attachment(url: str, max_bytes: int) -> bytes | None:
    """Download an attachment over HTTPS with redirect and size guards.

    Only HTTPS URLs whose hosts resolve exclusively to public addresses are
    fetched. Redirects are followed manually (at most ``MAX_REDIRECTS``) and
    revalidated, so a provider or CDN redirect cannot target loopback,
    link-local, or RFC1918 services. This is a check-before-connect guard
    (not DNS pinning): a resolver that changes answers between validation
    and connection could still rebind. The body is streamed and abandoned
    as soon as ``max_bytes`` is exceeded.
    """
    current = url
    validated: dict[str, bool] = {}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            for _ in range(MAX_REDIRECTS + 1):
                if not await validate_fetch_url(current, validated):
                    return None
                async with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            return None
                        current = urljoin(current, location)
                        continue
                    resp.raise_for_status()
                    return await read_capped_body(resp, max_bytes)
            logger.warning("Attachment redirect limit reached")
            return None
    except Exception as exc:
        host = urlparse(current).hostname or "unknown host"
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
