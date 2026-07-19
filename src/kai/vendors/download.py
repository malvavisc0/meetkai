"""Shared download helpers for vendor installers.

Centralizes HTTP streaming + progress reporting so each vendor installer
stays focused on its own layout. Uses httpx (already a project dep) rather
than shelling out to curl — one fewer runtime dependency and cross-platform.
"""

import logging
import os
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
    """Stream ``url`` to ``dest`` atomically, printing progress to the logger.

    Writes to ``dest.with_suffix(dest.suffix + ".part")`` and renames to
    ``dest`` only after the full stream completes and the byte count matches
    the server's ``Content-Length`` (when reported). This prevents a
    partially-downloaded file from being mistaken for a complete one on the
    next run — an interrupted download leaves only the ``.part`` file, which
    callers can detect and retry. Creates parent dirs. Returns ``dest``.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    own_client = client is None
    c = client or httpx.Client(follow_redirects=True, timeout=120.0)
    try:
        with c.stream("GET", url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", "0"))
            written = 0
            with part.open("wb") as f:
                for chunk in resp.iter_bytes(chunk_size=65536):
                    f.write(chunk)
                    written += len(chunk)
                    if total:
                        pct = written * 100 // total
                        logger.info("  %s  %3d%%  %s", dest.name, pct, _human(written))
                    elif written % (1024 * 1024) < 65536:
                        logger.info("  %s  %s", dest.name, _human(written))
            if total and written != total:
                part.unlink(missing_ok=True)
                raise RuntimeError(f"download of {url} truncated: wrote {written} of {total} bytes")
        # fsync before rename so the renamed file is fully on disk even if
        # the host crashes immediately after.
        with part.open("rb") as f:
            os.fsync(f.fileno())
        os.replace(part, dest)
        return dest
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        if own_client:
            c.close()


def remote_size(url: str, *, client: httpx.Client | None = None) -> int:
    """Return the ``Content-Length`` of ``url`` via a HEAD request, or 0 if
    the server doesn't report it. Used to validate that an existing file
    matches the expected size before skipping a download.
    """
    own_client = client is None
    c = client or httpx.Client(follow_redirects=True, timeout=30.0)
    try:
        r = c.head(url)
        r.raise_for_status()
        return int(r.headers.get("content-length", "0"))
    finally:
        if own_client:
            c.close()
