"""SSRF and file-type/size guards for Operator-submitted Brain ingestion input.

Two entry points into the Brain accept untrusted Operator input that is then
handed to shared, network-connected containers (crawl4ai, LightRAG):

- ``POST /brain/ingest-url`` — a URL that crawl4ai's headless Chromium fetches.
  Without validation an Operator can point this at any host reachable from
  the crawl4ai container's Docker network, including loopback, RFC1918
  private ranges, and cloud metadata endpoints (e.g. 169.254.169.254).
- ``POST /brain/upload`` — a file that is forwarded byte-for-byte to
  LightRAG's ``/documents/upload``, with no extension/size checks.

This module centralizes the checks for both. Callers should call these
*before* doing any network I/O against crawl4ai/LightRAG, and should treat
``ValueError`` as an Operator-facing message (the existing routes already
catch ``ValueError`` and surface ``str(exc)`` as a flash message).
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from typing import BinaryIO
from urllib.parse import urlparse

_ALLOWED_URL_SCHEMES = {"http", "https"}

# Extensions LightRAG's own docs list as supported ingest formats. Kept as an
# allowlist (not a denylist) so unknown/unexpected formats fail closed.
ALLOWED_UPLOAD_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".pdf",
        ".doc",
        ".docx",
        ".ppt",
        ".pptx",
        ".xls",
        ".xlsx",
        ".csv",
        ".rtf",
        ".odt",
        ".html",
        ".htm",
        ".json",
    }
)

# 25 MB — generous for text/office docs, small enough to bound memory/upload
# time and to match the spirit of the WAHA webhook's existing 1MB body cap
# (webhook payloads are tiny signed JSON; document uploads are naturally
# bigger, hence the larger but still bounded limit).
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def _is_disallowed_ip(ip: str) -> bool:
    """True if ``ip`` must not be reachable from an Operator-submitted URL."""
    addr = ipaddress.ip_address(ip)
    return (
        addr.is_private  # RFC1918 (10/8, 172.16/12, 192.168/16) + RFC4193
        or addr.is_loopback  # 127.0.0.0/8, ::1
        or addr.is_link_local  # 169.254.0.0/16 (incl. cloud metadata), fe80::/10
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified  # 0.0.0.0, ::
    )


def _resolve_all(host: str) -> list[str]:
    """Resolve every A/AAAA record for ``host``. Blocking — run via a thread."""
    infos = socket.getaddrinfo(host, None)
    return list({str(info[4][0]) for info in infos})


async def validate_ingest_url(url: str) -> None:
    """Validate an Operator-submitted URL before it is handed to crawl4ai.

    Rejects (raising ``ValueError`` with an Operator-safe message):
    - anything that isn't ``http://``/``https://`` or has no host,
    - hosts that fail to resolve,
    - hosts where *any* resolved A/AAAA record points at a private,
      loopback, link-local (this covers the 169.254.169.254 cloud metadata
      address), multicast, reserved, or unspecified address.

    This is checked at request time against live DNS (not just a static
    string pattern) so hostnames — not just raw IPs — are covered, and
    checking *every* resolved address (not just the first) closes the gap
    where a multi-A-record host resolves to a mix of public and internal
    IPs. It does not fully defeat DNS rebinding (the crawler may resolve
    again later), but it removes the trivial "paste a metadata/internal URL"
    attack, which is the realistic threat here.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_URL_SCHEMES:
        raise ValueError("URL must start with http:// or https://")
    host = parsed.hostname
    if not host:
        raise ValueError("URL must include a host.")

    try:
        ips = await asyncio.to_thread(_resolve_all, host)
    except socket.gaierror:
        raise ValueError(f"Could not resolve host: {host}") from None

    if not ips:
        raise ValueError(f"Could not resolve host: {host}")

    for ip in ips:
        if _is_disallowed_ip(ip):
            raise ValueError(
                "This URL resolves to a private, local, or reserved network "
                "address and cannot be crawled."
            )


def validate_upload_filename(filename: str) -> None:
    """Reject filenames whose extension isn't in the ingest allowlist."""
    _, ext = os.path.splitext(filename.lower())
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_UPLOAD_EXTENSIONS))
        raise ValueError(f"Unsupported file type '{ext or filename}'. Allowed types: {allowed}")


def validate_upload_size(file: BinaryIO, *, max_bytes: int = MAX_UPLOAD_BYTES) -> None:
    """Reject files over ``max_bytes``.

    Reads the stream's size via seek/tell rather than buffering the whole
    file into memory, then rewinds it so the caller can still read it from
    the start (e.g. to forward it to LightRAG).
    """
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size <= 0:
        raise ValueError("Uploaded file is empty.")
    if size > max_bytes:
        raise ValueError(
            f"File is too large ({size / (1024 * 1024):.1f} MB). "
            f"Max allowed is {max_bytes / (1024 * 1024):.0f} MB."
        )
