"""Email Inbox (Resend) connection CRUD — one Connection(service="resend") per operator.

The signing secret and API key are encrypted at rest. ``save`` with empty
fields preserves existing secrets. Probes just-saved credentials and sets
``status="connected"`` only when both sign and API are valid."""

import base64
import json
import logging
import time
import uuid

import httpx
from sqlalchemy.orm import Session

from kai.cockpit.connections.probe import (
    _is_transient_resend_error,
    reflect_probe_status,
)
from kai.cockpit.connections.secrets import decrypt_config, encrypt_config
from kai.cockpit.connections.webhooks import _RESEND_API_BASE, _sign_resend, _strip_whsec_prefix
from kai.cockpit.models import Connection, User
from kai.utils.common import now_iso

logger = logging.getLogger(__name__)


class EmailConnectionsService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, user: User) -> Connection | None:
        return (
            self.db.query(Connection)
            .filter(Connection.user_id == user.id, Connection.service == "resend")
            .first()
        )

    def save(self, user: User, *, signing_secret: str, api_key: str) -> Connection:
        existing = self.get(user)
        secrets = {}
        if signing_secret:
            secrets["signing_secret"] = signing_secret
        if api_key:
            secrets["api_key"] = api_key
        if existing is None:
            conn = Connection(
                user_id=user.id,
                service="resend",
                status="disconnected",
                config=encrypt_config("resend", secrets) if secrets else {},
                created_at=now_iso(),
                updated_at=now_iso(),
            )
            self.db.add(conn)
        else:
            if secrets:
                merged = {**decrypt_config("resend", existing.config), **secrets}
                existing.config = encrypt_config("resend", merged)
            existing.updated_at = now_iso()
            conn = existing
        self.db.commit()
        self.db.refresh(conn)

        # Probe the just-saved credentials and reflect the result in ``status``.
        # Transient failures preserve the prior status.
        ok, _, transient = self._verify_conn(conn)
        reflect_probe_status(self.db, conn, ok, transient=transient)
        return conn

    def delete(self, user: User) -> None:
        conn = self.get(user)
        if conn is not None:
            self.db.delete(conn)
            self.db.commit()

    def decrypt_secret(self, user: User) -> str | None:
        conn = self.get(user)
        if conn is None:
            return None
        return decrypt_config("resend", conn.config).get("signing_secret")

    def decrypt_api_key(self, user: User) -> str | None:
        conn = self.get(user)
        if conn is None:
            return None
        return decrypt_config("resend", conn.config).get("api_key")

    def verify(self, user: User) -> tuple[bool, str]:
        """Validate the persisted Resend credentials. Returns ``(ok, message)``."""
        conn = self.get(user)
        if conn is None:
            return False, "no Resend connection configured"
        ok, msg, _ = self._verify_conn(conn)
        return ok, msg

    def _verify_conn(self, conn: Connection) -> tuple[bool, str, bool]:
        """Validate persisted Resend credentials.

        Two checks, both must pass:
          1. Signing secret is well-formed base64 (after stripping ``whsec_`` prefix).
          2. API key is accepted by Resend (``GET /domains``).

        Returns ``(ok, message, transient)`` — ``transient`` for
        network/timeout/429/5xx, False for auth rejection.
        """
        cfg = decrypt_config("resend", conn.config)
        secret = cfg.get("signing_secret", "")
        api_key = cfg.get("api_key", "")
        if not secret:
            return False, "no signing secret configured", False
        if not api_key:
            return False, "no API key configured", False
        try:
            base64.b64decode(_strip_whsec_prefix(secret))
        except Exception as exc:
            return False, f"signing secret is not valid base64: {exc}", False
        try:
            resp = httpx.get(
                f"{_RESEND_API_BASE}/domains",
                headers={"Authorization": f"Bearer {api_key}"},
                params={"limit": 1},
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            return False, f"could not reach Resend API: {exc}", True
        if resp.status_code in (401, 403):
            return False, f"Resend API rejected the key ({resp.status_code})", False
        if resp.status_code != 200:
            transient = _is_transient_resend_error(resp.status_code, None)
            return False, f"Resend API returned {resp.status_code}", transient
        return True, "ok", False

    def test(
        self,
        user: User,
        *,
        base_url: str,
        signing_secret: str | None = None,
    ) -> tuple[bool, str]:
        """Self-loopback test: sign a sample Resend payload and POST to our own ingress.

        Uses the provided ``signing_secret`` (ad-hoc) or the persisted secret.
        Builds a sample ``email.received`` webhook body, signs it with the
        Svix scheme, and POSTs to ``/webhook/{user.kai_slug}/resend``.

        The synthetic payload's ``email_id`` isn't real, so the route's
        Resend-API body-fetch will 502. That 502 is a **pass**: it proves
        signature verification, nonce handling, and bot routing all worked.
        Only a real inbound email tests that leg end to end.

        ``base_url`` is the cockpit's address as seen by the incoming request —
        the bind host/port is only known at process-start time, so deriving
        the loopback target from the request is the only way to hit the right
        address instead of guessing a port.
        """
        secret = signing_secret or self.decrypt_secret(user)
        if not secret:
            return False, "no signing secret configured"

        svix_id = f"test-loopback-{uuid.uuid4().hex[:8]}"
        ts = int(time.time())
        body = json.dumps(
            {
                "type": "email.received",
                "created_at": now_iso(),
                "data": {
                    "email_id": f"test-loopback-{uuid.uuid4()}",
                    "created_at": now_iso(),
                    "from": "test@meetk.ai",
                    "to": ["support@meetk.ai"],
                    "bcc": [],
                    "cc": [],
                    "received_for": ["support@meetk.ai"],
                    "message_id": f"<{svix_id}@meetk.ai>",
                    "subject": "connection test",
                    "attachments": [],
                },
            }
        ).encode()

        try:
            signature = _sign_resend(svix_id, str(ts), body, secret)
        except Exception as exc:
            return False, f"secret is not valid base64: {exc}"

        webhook_url = f"{base_url.rstrip('/')}/webhook/{user.kai_slug}/resend"
        try:
            resp = httpx.post(
                webhook_url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "svix-id": svix_id,
                    "svix-timestamp": str(ts),
                    "svix-signature": signature,
                },
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            return False, f"could not reach ingress: {exc}"

        if resp.status_code == 202:
            return True, "ok — webhook verified and forwarded"
        if resp.status_code == 401:
            return False, "signature verification failed (wrong secret?)"
        if resp.status_code == 404:
            return False, "no running email bot to receive the event (404)"
        if resp.status_code == 502:
            detail = ""
            try:
                detail = resp.json().get("detail", "")
            except Exception:
                pass
            if "upstream provider API error" in detail:
                return True, (
                    "ok — signature verified and event routed to the bot. "
                    "The synthetic test email can't be fetched from Resend "
                    "(expected — it isn't a real email); send a real email "
                    "to test the full body-fetch path."
                )
            return False, "bot received the event but rejected it (502)"
        return False, f"unexpected status {resp.status_code}"
