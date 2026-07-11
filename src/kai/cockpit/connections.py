"""Shared connections service — WAHA QR connect/disconnect + Connection CRUD.

Account-level integration management. v1: WhatsApp only. Future: github,
telegram, etc. Used by both CLI and web routes.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kai.bots.waha.client import WahaClient
from kai.bots.waha.config import get_waha_settings
from kai.cockpit.models import Connection, User
from kai.utils.common import now_iso, user_slug

logger = logging.getLogger(__name__)

# Stop polling for a QR/session once this many consecutive WAHA calls fail —
# otherwise a broken session or auth error spins the loop for all 60 iters
# silently. A handful of transient blips are still tolerated.
_MAX_CONSECUTIVE_FAILURES = 5


class ConnectionsService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_for_user(self, user: User) -> list[Connection]:
        """Every connection row for this operator (all services).

        The generic "what does this operator have?" read the catalog-driven
        code needs — the settings UI and any future "show all connections"
        page use this instead of a raw query in the route. Per-type helpers
        (``get_whatsapp``, ``get_brain``, future ``get_database``) stay as
        the bespoke read path for a single known service.
        """
        return (
            self.db.query(Connection)
            .filter(Connection.user_id == user.id)
            .all()
        )

    def get_whatsapp(self, user: User) -> Connection | None:
        """Get the user's WhatsApp connection row, or None."""
        return (
            self.db.query(Connection)
            .filter(Connection.user_id == user.id, Connection.service == "whatsapp")
            .first()
        )

    def get_or_create_whatsapp(self, user: User) -> Connection:
        """Get or lazily create the user's WhatsApp connection row."""
        conn = self.get_whatsapp(user)
        if conn is not None:
            return conn

        session_name = user_slug(user.kai_slug)
        port_range = os.environ.get("KAI_COCKPIT_WEBHOOK_PORT_RANGE", "8100-8199")
        parts = port_range.split("-")
        lo = int(parts[0])
        hi = int(parts[1]) if len(parts) > 1 else lo + 100

        for attempt in range(hi - lo + 1):
            port = self._pick_free_port(lo, hi)
            conn = Connection(
                user_id=user.id,
                service="whatsapp",
                status="disconnected",
                config={
                    "waha_session": session_name,
                    "waha_webhook_port": port,
                    "waha_webhook_path": f"/webhook/whatsapp/{user.id}",
                },
                webhook_port=port,
                created_at=now_iso(),
                updated_at=now_iso(),
            )
            self.db.add(conn)
            try:
                self.db.commit()
                self.db.refresh(conn)
                return conn
            except IntegrityError:
                # Another concurrent request committed the same port first
                # (enforced by the `connections.webhook_port` unique index).
                # Roll back and retry with a freshly recomputed free port.
                self.db.rollback()
                continue

        raise RuntimeError(f"Could not allocate a port in range {port_range}")

    async def connect_whatsapp(self, user: User) -> dict:
        """Start WAHA session + set webhook + poll for QR status.

        Returns {"status": "connected"} or
        {"status": "scan_qr", "qr_bytes": bytes}.
        """
        conn = self.get_or_create_whatsapp(user)

        public_host = os.environ.get("KAI_WAHA_WEBHOOK_PUBLIC_HOST", "")
        webhook_url = (
            f"http://{public_host}:"
            f"{conn.config['waha_webhook_port']}"
            f"{conn.config['waha_webhook_path']}"
        )

        client = WahaClient(get_waha_settings())
        try:
            await client.create_session(
                name=conn.config["waha_session"],
                webhook_config={
                    "url": webhook_url,
                    "events": ["message"],
                    "hmac": {"key": user.hmac_key, "algorithm": "sha512"},
                },
            )

            conn.status = "connecting"
            conn.updated_at = now_iso()
            self.db.commit()

            # Give WAHA a moment to transition out of STARTING before the
            # first status probe — querying too quickly can race the session
            # into a half-initialized state where get_qr 422s.
            await asyncio.sleep(1)

            consecutive_failures = 0
            for _ in range(60):
                try:
                    session_info = await client.get_session(conn.config["waha_session"])
                    consecutive_failures = 0
                except Exception:
                    consecutive_failures += 1
                    logger.warning(
                        "connect_whatsapp: get_session failed (%d/%d) for %s",
                        consecutive_failures,
                        _MAX_CONSECUTIVE_FAILURES,
                        conn.config["waha_session"],
                        exc_info=True,
                    )
                    if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                        raise ConnectionError("WAHA session unreachable after repeated failures")
                    await asyncio.sleep(1)
                    continue

                if not session_info:
                    break

                status = session_info.get("status", "")

                if status == "WORKING":
                    conn.status = "connected"
                    conn.updated_at = now_iso()
                    self.db.commit()
                    return {"status": "connected"}

                if status in ("SCAN_QR_CODE", "STARTING"):
                    try:
                        qr_bytes = await client.get_qr(conn.config["waha_session"])
                        return {"status": "scan_qr", "qr_bytes": qr_bytes}
                    except Exception:
                        logger.warning(
                            "connect_whatsapp: get_qr failed for %s",
                            conn.config["waha_session"],
                            exc_info=True,
                        )

                if status == "STOPPED":
                    # Session exists but isn't running (QR expired, prior
                    # stop, or create_session didn't auto-start). Nudge it
                    # back to SCAN_QR_CODE instead of polling a dead state.
                    try:
                        await client.start_session(conn.config["waha_session"])
                    except Exception:
                        logger.warning(
                            "connect_whatsapp: start_session failed for %s",
                            conn.config["waha_session"],
                            exc_info=True,
                        )

                if status == "FAILED":
                    conn.status = "disconnected"
                    self.db.commit()
                    raise ConnectionError("WAHA session failed")

                await asyncio.sleep(1)

            return {"status": "connecting"}
        finally:
            await client.close()

    async def get_qr(self, user: User) -> bytes | None:
        """Fetch current QR code image bytes for the user's WAHA session.

        If the session is STOPPED (QR expired or never started), restart it
        first so it re-enters SCAN_QR_CODE — otherwise the QR endpoint 422s.
        Returns None if the user has no WhatsApp connection (so the route can
        answer 404); WAHA-side failures are logged, not silenced.
        """
        conn = self.get_whatsapp(user)
        if not conn or conn.status == "disconnected":
            return None

        client = WahaClient(get_waha_settings())
        try:
            session_info = await client.get_session(conn.config["waha_session"])
            if session_info and session_info.get("status") == "STOPPED":
                try:
                    await client.start_session(conn.config["waha_session"])
                except Exception:
                    logger.warning(
                        "get_qr: start_session failed for %s",
                        conn.config["waha_session"],
                        exc_info=True,
                    )
            return await client.get_qr(conn.config["waha_session"])
        except Exception:
            logger.warning(
                "get_qr: failed to fetch QR for %s",
                conn.config["waha_session"],
                exc_info=True,
            )
            return None
        finally:
            await client.close()

    async def refresh_status(self, user: User) -> Connection:
        """Poll WAHA session status, update Connection row."""
        conn = self.get_whatsapp(user)
        if not conn:
            return self.get_or_create_whatsapp(user)

        client = WahaClient(get_waha_settings())
        try:
            session_info = await client.get_session(conn.config["waha_session"])
        finally:
            await client.close()

        if not session_info:
            conn.status = "disconnected"
        else:
            wa_status = session_info.get("status", "")
            if wa_status == "WORKING":
                conn.status = "connected"
            elif wa_status in ("STARTING", "SCAN_QR_CODE"):
                conn.status = "connecting"
            else:
                conn.status = "disconnected"

        conn.updated_at = now_iso()
        self.db.commit()
        return conn

    async def disconnect_whatsapp(self, user: User) -> None:
        """Delete WAHA session, set Connection status to disconnected."""
        conn = self.get_whatsapp(user)
        if not conn or conn.status == "disconnected":
            return

        client = WahaClient(get_waha_settings())
        try:
            await client.delete_session(conn.config["waha_session"])
        finally:
            await client.close()

        conn.status = "disconnected"
        conn.updated_at = now_iso()
        self.db.commit()

    # --- Port allocation ---

    def _pick_free_port(self, lo: int, hi: int) -> int:
        """Pick the next unused port from the given range."""
        used_ports: set[int] = set()
        whatsapp_conns = self.db.query(Connection).filter(Connection.service == "whatsapp").all()
        for c in whatsapp_conns:
            port = c.config.get("waha_webhook_port")
            if isinstance(port, int):
                used_ports.add(port)

        for port in range(lo, hi + 1):
            if port not in used_ports:
                return port

        raise RuntimeError(f"no available ports in range {lo}-{hi} ({len(used_ports)} in use)")
