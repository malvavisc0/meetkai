"""Email Inbox (Resend) connection CRUD — one Connection(service="resend") per operator.

The signing secret is encrypted at rest via ``encrypt_config`` /
``decrypt_config`` (the ``signing_secret`` field is listed in
``WEBHOOK_CONNECTION_TYPES["resend"].secret_fields``).

``save`` with an empty ``signing_secret`` preserves the existing encrypted
secret — the form shows ``••••••••`` when a secret is already stored, so
the operator never re-types it on a no-op edit.

``save`` probes the just-saved credentials (signing-secret base64
validity + API key accepted by Resend's ``GET /domains``) and sets
``status="connected"`` only when both pass — otherwise
``status="disconnected"``. The self-loopback ``test()`` remains an
explicit operator action for exercising the full ingress + bot path.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid

import httpx
from sqlalchemy.orm import Session

from kai.cockpit.connection_probe import (
    _is_transient_resend_error,
    reflect_probe_status,
)
from kai.cockpit.models import Connection, User
from kai.cockpit.secrets import decrypt_config, encrypt_config
from kai.cockpit.webhooks import _RESEND_API_BASE, _sign_resend, _strip_whsec_prefix
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

        # Probe the just-saved credentials and reflect the result in
        # ``status``. Transient failures (network/timeout/429) preserve the
        # prior status; auth rejections mark the connection
        # ``disconnected``. Passes the already-loaded ``conn`` to
        # ``_verify_conn`` to avoid a redundant SELECT.
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
        """Validate the persisted Resend credentials without a running bot.

        Returns ``(ok, message)``. See ``_verify_conn`` for the full
        ``(ok, message, transient)`` return used by ``save()``.
        """
        conn = self.get(user)
        if conn is None:
            return False, "no Resend connection configured"
        ok, msg, _ = self._verify_conn(conn)
        return ok, msg

    def _verify_conn(self, conn: Connection) -> tuple[bool, str, bool]:
        """Validate persisted Resend credentials from an already-loaded
        ``Connection`` row. Returns ``(ok, message, transient)``.

        Two independent checks, both must pass:
          1. The signing secret is well-formed base64 (after stripping the
             ``whsec_`` prefix Resend prepends).
          2. The API key is accepted by Resend's API (``GET /domains``).

        ``transient`` is True when the failure is a network/timeout/429/5xx
        issue (preserve prior status) rather than an auth rejection (401/403
        or malformed secret → mark ``disconnected``).
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
        """Self-loopback test: sign a sample Resend payload and POST to our own ingress route.

        Uses the provided ``signing_secret`` (ad-hoc) or the persisted secret.
        Builds a sample ``email.received`` webhook body (the real envelope
        shape — ``type``/``created_at``/``data``), signs it with the Svix
        scheme (reusing the exact signing logic from ``_verify_resend`` in
        ``webhooks.py``), and POSTs to ``/webhook/{user.kai_slug}/resend``.

        The synthetic payload's ``data.email_id`` isn't a real Resend email,
        so the route's Resend-API body-fetch (required because the webhook
        itself never carries the body — see ``_parse_resend``) will 502.
        That 502 is treated as a **pass**: it proves signature verification,
        nonce handling, and bot routing all worked — the only thing that
        can't be exercised locally is fetching a body that doesn't exist.
        Only a real inbound email tests that leg end to end.

        ``base_url`` is the cockpit's own address as seen by the incoming
        request (``str(request.base_url)``) — the cockpit's bind host/port
        is only known at process-start time (``cockpit serve --host/--port``),
        so deriving the loopback target from the request itself is the only
        way to hit the right address instead of guessing a port.
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
