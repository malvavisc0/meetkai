"""SSRF and file-type/size guards for Operator-submitted Brain ingestion input.

Two entry points into the Brain accept untrusted Operator input handed to
shared, network-connected containers (crawl4ai, LightRAG):

- ``POST /brain/ingest-url`` — a URL crawl4ai's headless Chromium fetches.
- ``POST /brain/upload`` — a file forwarded byte-for-byte to LightRAG.

Callers should run these checks *before* any network I/O; treat ``ValueError``
as an Operator-facing message.
"""

import asyncio
import ipaddress
import os
import socket
from typing import BinaryIO
from urllib.parse import urlparse

_ALLOWED_URL_SCHEMES = {"http", "https"}

# Extensions LightRAG's own docs list as supported ingest formats (allowlist).
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

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB — generous for text/office docs


def _is_disallowed_ip(ip: str) -> bool:
    """True if ``ip`` must not be reachable from an Operator-submitted URL."""
    addr = ipaddress.ip_address(ip)
    return (
        addr.is_private  # private ranges (10.x, 172.16-31.x, 192.168.x)
        or addr.is_loopback  # 127.0.0.0/8, ::1
        or addr.is_link_local  # link-local (includes cloud metadata endpoints)
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified  # unset address (0.0.0.0, ::)
    )


def _resolve_all(host: str) -> list[str]:
    """Resolve every A/AAAA record for ``host``. Blocking — run via a thread."""
    infos = socket.getaddrinfo(host, None)
    return list({str(info[4][0]) for info in infos})


async def validate_ingest_url(url: str) -> None:
    """Validate an Operator-submitted URL before handing it to crawl4ai.

    Rejects (raising ``ValueError``):
    - anything that isn't ``http://``/``https://`` or has no host,
    - hosts that fail to resolve,
    - hosts where *any* resolved A/AAAA record points at a private, loopback,
      link-local, multicast, reserved, or unspecified address.

    Checked at request time against live DNS (not a static string pattern).
    Checking *every* resolved address closes the gap where a multi-A-record
    host resolves to a mix of public and internal IPs. It does not fully
    defeat DNS rebinding, but removes the trivial metadata/internal URL attack.
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
