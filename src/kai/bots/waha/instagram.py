"""Instagram post enrichment for the WAHA bot.

When an inbound message contains an Instagram post/reel URL, fetch the
post's metadata and images so the agent can see the picture and read the
caption in the same turn. This is input pre-processing (same role as
voice-note transcription), not an LLM tool.

Two-step approach:
1. curl_cffi (impersonate="chrome") fetches the post page → parse data-sjs
   JSON for metadata + CDN image URLs (scontent-*.cdninstagram.com).
   curl_cffi is needed because Instagram blocks non-browser TLS fingerprints;
   impersonate="chrome" mimics Chrome's TLS handshake.
2. httpx.get(CDN_url) → image bytes. The CDN is publicly accessible, no auth.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Match post/reel/reels/tv URLs and capture the shortcode.
_INSTA_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/"
    r"(?:p|reel|reels|tv)/(?P<shortcode>[A-Za-z0-9_-]+)"
)

_PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.instagram.com/",
}

_CDN_HEADERS = {"User-Agent": "Mozilla/5.0"}

_MAX_IMAGES = 4  # cap per post to protect LLM token budget
_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB, matches MediaConfig.max_size_mb


def extract_instagram_shortcode(text: str) -> str | None:
    """Return the post shortcode if text contains an IG post/reel URL, else None."""
    m = _INSTA_RE.search(text)
    return m.group("shortcode") if m else None


def fetch_instagram_post(shortcode: str) -> tuple[str, list[bytes]]:
    """Fetch a single Instagram post by shortcode.

    Returns ``(post_data_text, image_bytes_list)``. ``image_bytes_list`` is
    empty for video-only posts or on download failure — callers should still
    use the text. Raises on hard failure so the async wrapper can log and
    degrade gracefully.

    Uses curl_cffi (Chrome TLS impersonation) to fetch the SSR post page,
    parses the embedded ``data-sjs`` JSON for media metadata + CDN URLs,
    then downloads each image from the CDN with httpx (no auth needed).
    """
    url = f"https://www.instagram.com/p/{shortcode}/"
    # Imported lazily so a missing curl_cffi degrades gracefully (see the
    # availability probe in Bot._enrich_instagram) instead of crashing the
    # bot at import time.
    from curl_cffi import requests as curl_requests

    resp = curl_requests.get(url, headers=_PAGE_HEADERS, impersonate="chrome", timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"Instagram returned HTTP {resp.status_code}")

    media = _extract_media_node(resp.text, shortcode)
    if media is None:
        raise RuntimeError("could not find media node in page HTML")

    data_text = _build_data_text(media, shortcode)
    cdn_urls = _extract_cdn_urls(media)
    image_bytes_list = _download_images(cdn_urls[:_MAX_IMAGES])
    return data_text, image_bytes_list


def _extract_media_node(html: str, shortcode: str) -> dict | None:
    """Parse data-sjs JSON blocks and walk for the media node.

    Looks for a dict with key ``xig_polaris_media`` whose
    ``if_not_gated_logged_out`` child has ``code == shortcode``. Walking
    the tree (rather than hard-coding the JSON path) is robust to Instagram
    reordering the require[] nesting.
    """
    blocks = re.findall(
        r'<script type="application/json"[^>]*data-sjs>(.*?)</script>',
        html,
        re.DOTALL,
    )
    for raw in blocks:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        node = _walk_for_media(data, shortcode)
        if node is not None:
            return node
    return None


def _walk_for_media(obj: Any, shortcode: str) -> dict | None:
    """Recursively search for the xig_polaris_media node matching shortcode."""
    if isinstance(obj, dict):
        polaris = obj.get("xig_polaris_media")
        if isinstance(polaris, dict):
            node = polaris.get("if_not_gated_logged_out")
            if isinstance(node, dict) and node.get("code") == shortcode:
                return node
        for v in obj.values():
            found = _walk_for_media(v, shortcode)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _walk_for_media(v, shortcode)
            if found is not None:
                return found
    return None


def _build_data_text(media: dict, shortcode: str) -> str:
    """Build the tagged text summary for the agent."""
    typename = media.get("__typename", "unknown")
    user = media.get("user") or {}
    caption_obj = media.get("caption") or {}
    caption = caption_obj.get("text", "") if isinstance(caption_obj, dict) else str(caption_obj)

    lines = [
        f"by @{user.get('username', 'unknown')}",
        f"type: {_typename_label(typename)}",
        f"code: {shortcode}",
        f"caption: {caption or '(none)'}",
    ]
    for k in ("like_count", "comment_count"):
        if k in media:
            lines.append(f"{k}: {media[k]}")
    lines.append(f"url: https://www.instagram.com/p/{shortcode}/")
    return "\n  ".join(lines)


def _typename_label(typename: str) -> str:
    if "Carousel" in typename:
        return "carousel"
    if "Video" in typename:
        return "video"
    if "Image" in typename:
        return "photo"
    return typename


def _extract_cdn_urls(media: dict) -> list[str]:
    """Extract CDN image URLs from the media node.

    For carousels, returns ``display_uri`` of every carousel item (including
    video poster frames). For single posts, returns ``media.display_uri``.
    """
    typename = media.get("__typename", "")
    urls: list[str] = []
    if "Carousel" in typename:
        for item in media.get("carousel_media", []):
            uri = item.get("display_uri")
            if uri:
                urls.append(uri)
    elif media.get("display_uri"):
        urls.append(media["display_uri"])
    return urls


def _download_images(cdn_urls: list[str]) -> list[bytes]:
    """Download images from public CDN URLs via httpx."""
    images: list[bytes] = []
    for url in cdn_urls:
        try:
            with httpx.Client(headers=_CDN_HEADERS, timeout=30.0, follow_redirects=True) as client:
                resp = client.get(url)
                resp.raise_for_status()
                if len(resp.content) <= _MAX_IMAGE_BYTES:
                    images.append(resp.content)
                else:
                    logger.warning("IG image too large (%d bytes), skipping", len(resp.content))
        except Exception:
            logger.warning("failed to download IG image %s", url, exc_info=True)
    return images
