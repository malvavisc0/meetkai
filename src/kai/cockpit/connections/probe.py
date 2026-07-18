"""Shared helpers for connection services.

The save-time probe pattern (commit config → probe → reflect status) is
identical across SMTP, Database, and Resend connection services. These
helpers centralize status-reflection and the transient-vs-auth-rejection
distinction."""

from __future__ import annotations

import logging
import smtplib
import socket
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError

from sqlalchemy.orm import Session

from kai.cockpit.models import Connection
from kai.utils.common import now_iso

logger = logging.getLogger(__name__)

# Overall wall-clock budget for a save-time SMTP probe. smtplib's per-
# socket-operation timeout doesn't cover DNS resolution or the full
# handshake, so a dead host can block 50s+.
_SMTP_PROBE_TIMEOUT = 15


def _is_transient_smtp_error(exc: Exception) -> bool:
    """True if the SMTP failure is transient (network/timeout), not an
    auth rejection."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return False
    if isinstance(exc, smtplib.SMTPConnectError):
        return True
    if isinstance(exc, (socket.timeout, ConnectionRefusedError, ConnectionResetError)):
        return True
    if isinstance(exc, socket.gaierror):
        return True  # DNS failure — transient
    if isinstance(exc, OSError):
        return True  # network-level error
    return False


def _is_transient_db_error(exc: Exception) -> bool:
    """True if the database failure is transient (network/timeout), not an
    auth rejection or invalid DSN."""
    exc_name = type(exc).__name__
    if exc_name in ("OperationalError",):
        msg = str(exc).lower()
        if any(k in msg for k in ("authentication", "password", "access denied", "permission")):
            return False
        return True  # connection refused, timeout, host unreachable
    if exc_name == "ModuleNotFoundError":
        return False  # missing DBAPI driver — a config issue, not transient
    return False


def _is_transient_resend_error(status_code: int | None, exc: Exception | None) -> bool:
    """True if the Resend API failure is transient (network/429/5xx), not
    an auth rejection (401/403)."""
    if exc is not None:
        return True  # httpx.ConnectError, TimeoutException, etc.
    if status_code == 429:
        return True  # rate-limited upstream
    if status_code is not None and 500 <= status_code < 600:
        return True  # server error
    return False


def reflect_probe_status(
    db: Session,
    conn: Connection,
    ok: bool,
    *,
    transient: bool = False,
) -> Connection:
    """Set ``conn.status`` based on probe result and commit.

    - ``ok=True`` → ``connected``
    - ``ok=False, transient=False`` → ``disconnected`` (auth rejection)
    - ``ok=False, transient=True`` → preserve prior status (transient failure)
    """
    if ok:
        conn.status = "connected"
    elif transient and conn.status == "connected":
        logger.warning(
            "probe failed transiently for %s connection — preserving prior 'connected' status",
            conn.service,
        )
    else:
        conn.status = "disconnected"
    conn.updated_at = now_iso()
    db.commit()
    db.refresh(conn)
    return conn


def run_smtp_probe_with_timeout(
    host: str, port: int, username: str, password: str, use_tls: bool
) -> tuple[bool, str, bool]:
    """Run the SMTP handshake probe with an overall wall-clock timeout.

    Returns ``(ok, message, transient)``. Uses a manual ThreadPoolExecutor
    so on timeout the pool shuts down without blocking the calling thread.
    """
    from kai.cockpit.connections.smtp import _smtp_test

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_smtp_test, host, port, username, password, use_tls)
    try:
        ok, msg = future.result(timeout=_SMTP_PROBE_TIMEOUT)
        pool.shutdown(wait=True)
        return ok, msg, False
    except FuturesTimeoutError:
        pool.shutdown(wait=False, cancel_futures=True)
        return False, "probe timed out", True
    except Exception as exc:
        pool.shutdown(wait=True)
        return False, str(exc), _is_transient_smtp_error(exc)
