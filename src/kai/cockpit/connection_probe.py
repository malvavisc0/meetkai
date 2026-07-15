"""Shared helpers for connection services.

The save-time probe pattern (commit config → probe → reflect status) is
identical across SMTP, Database, and Resend connection services. These
helpers centralize the status-reflection logic and the distinction between
auth-rejection (credentials are wrong → set ``disconnected``) and
transient failures (network/timeout/rate-limit → preserve prior status so
a blip doesn't block deploys).
"""

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
# socket-operation timeout (10s) doesn't cover DNS resolution or the full
# handshake sequence, so a dead host can block 50s+. This caps the total.
_SMTP_PROBE_TIMEOUT = 15


def _is_transient_smtp_error(exc: Exception) -> bool:
    """True if the SMTP failure is a network/timeout issue, not an auth
    rejection. Transient errors should preserve the prior status rather
    than marking the connection ``disconnected``."""
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
    """True if the database failure is a network/timeout issue, not an auth
    rejection or invalid DSN."""
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
    """True if the Resend API failure is transient (network/429/5xx), not an
    auth rejection (401/403)."""
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
    """Set ``conn.status`` based on the probe result and commit.

    - ``ok=True`` → ``status="connected"``
    - ``ok=False, transient=False`` → ``status="disconnected"`` (auth
      rejection or invalid credentials — the credential is genuinely wrong)
    - ``ok=False, transient=True`` → preserve the prior ``status`` (network
      blip, timeout, rate-limit — the credential may be valid; don't
      downgrade a previously-``connected`` row)

    Always updates ``updated_at`` and commits + refreshes.
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

    Returns ``(ok, message, transient)``. ``transient`` is True when the
    failure is a network/timeout issue (not an auth rejection), so the
    caller can preserve the prior connection status instead of downgrading.

    Uses a manual ThreadPoolExecutor (not a ``with`` block) so that on
    timeout the pool can be shut down with ``wait=False`` — the running
    probe's orphaned worker continues until its socket op times out, but
    the calling thread is not blocked past the 15s cap.
    """
    from kai.cockpit.smtp_connections import _smtp_test

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


def flash_connection_save(request, service_label: str, conn: Connection) -> None:
    """Set the flash message for a connection save based on the probe
    result reflected in ``conn.status``. Shared by the SMTP, Database,
    and Resend save routes so the wording stays consistent."""
    if conn.status == "connected":
        request.session["flash"] = f"{service_label} connection saved and verified."
    else:
        request.session["flash"] = (
            f"{service_label} connection saved but could not be verified — "
            "use Test connection to see the error."
        )
