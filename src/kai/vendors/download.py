"""Shared download helpers for vendor installers.

Centralizes HTTP streaming + progress reporting so each vendor installer
stays focused on its own layout. Uses httpx (already a project dep) rather
than shelling out to curl — one fewer runtime dependency and cross-platform.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def download(url: str, dest: Path, *, client: httpx.Client | None = None) -> Path:
    """Stream ``url`` to ``dest``, printing progress to the logger.

    Overwrites ``dest``. Creates parent dirs. Returns ``dest``.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    own_client = client is None
    c = client or httpx.Client(follow_redirects=True, timeout=120.0)
    try:
        with c.stream("GET", url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", "0"))
            written = 0
            with dest.open("wb") as f:
                for chunk in resp.iter_bytes(chunk_size=65536):
                    f.write(chunk)
                    written += len(chunk)
                    if total:
                        pct = written * 100 // total
                        logger.info("  %s  %3d%%  %s", dest.name, pct, _human(written))
                    elif written % (1024 * 1024) < 65536:
                        logger.info("  %s  %s", dest.name, _human(written))
            return dest
    finally:
        if own_client:
            c.close()
