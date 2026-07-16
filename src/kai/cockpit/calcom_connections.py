"""Cal.com connection CRUD — one Connection(service="calcom") per operator.

The API key is encrypted at rest via ``encrypt_config`` /
``decrypt_config`` (the ``api_key`` field is listed in
``CREDENTIAL_TYPES["calcom"].secret_fields``). ``base_url`` stays plaintext
for template rendering.

``save`` with an empty ``api_key`` preserves the existing encrypted key —
the form shows ``••••••••`` when a key is already stored, so the operator
never re-types it on a base_url-only edit.

Cal.com v2 authenticates with a static Bearer API key (prefixed ``cal_``),
so this is a credential connection in the same shape as ``database`` /
``smtp`` — no OAuth, no webhook ingress.
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy.orm import Session

from kai.cockpit.connection_probe import reflect_probe_status
from kai.cockpit.models import Connection, User
from kai.cockpit.secrets import decrypt_config, encrypt_config
from kai.utils.common import now_iso

logger = logging.getLogger(__name__)

# The default Cal.com v2 API host. ``base_url`` is optional in the catalog
# (an operator overrides it for a self-hosted instance), so every caller
# applies this fallback when the stored value is blank — Database/SMTP dodge
# this because all their fields are required; Cal.com does not.
DEFAULT_BASE_URL = "https://api.cal.com/v2"


class CalcomConnectionsService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, user: User) -> Connection | None:
        return (
            self.db.query(Connection)
            .filter(Connection.user_id == user.id, Connection.service == "calcom")
            .first()
        )

    def save(self, user: User, *, api_key: str, base_url: str) -> Connection:
        existing = self.get(user)
        config: dict = {"base_url": base_url.strip()}
        if api_key:
            config["api_key"] = api_key
        encrypted = encrypt_config("calcom", config)
        if existing is None:
            conn = Connection(
                user_id=user.id,
                service="calcom",
                status="disconnected",
                config=encrypted,
                created_at=now_iso(),
                updated_at=now_iso(),
            )
            self.db.add(conn)
        else:
            if api_key:
                existing.config = encrypted
            else:
                # Preserve existing encrypted key; merge the plaintext field.
                existing.config = {
                    **existing.config,
                    "base_url": base_url.strip(),
                }
            existing.updated_at = now_iso()
            conn = existing
        self.db.commit()
        self.db.refresh(conn)

        # Probe the just-saved credentials and reflect the result in
        # ``status``. Transient failures (network/timeout) preserve the
        # prior status so a blip doesn't block deploys; only auth
        # rejections mark the connection ``disconnected``. Decrypts the
        # just-persisted config to avoid a redundant SELECT.
        cfg = decrypt_config("calcom", conn.config)
        ok, _, transient = self._probe(
            cfg.get("api_key", ""),
            cfg.get("base_url", ""),
        )
        reflect_probe_status(self.db, conn, ok, transient=transient)
        return conn

    def delete(self, user: User) -> None:
        conn = self.get(user)
        if conn is not None:
            self.db.delete(conn)
            self.db.commit()

    def decrypt_api_key(self, user: User) -> str | None:
        conn = self.get(user)
        if conn is None:
            return None
        return decrypt_config("calcom", conn.config).get("api_key")

    def _probe(self, api_key: str, base_url: str) -> tuple[bool, str, bool]:
        """Call Cal.com's ``GET /v2/me`` to validate the API key. Returns
        ``(ok, message, transient)`` — ``transient`` is True when the
        failure is a network/timeout/rate-limit issue (preserve prior
        status) rather than an auth rejection (mark ``disconnected``).

        ``GET /v2/me`` does not require a ``cal-api-version`` header (unlike
        the event-types/slots/bookings endpoints the agent tool calls — see
        ``agent/tools/calcom.py``); it accepts only the Bearer token.
        """
        url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        try:
            resp = httpx.get(
                f"{url}/me",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
        except Exception as exc:
            return False, f"could not reach Cal.com API: {exc}", True
        if resp.status_code in (401, 403):
            return False, f"Cal.com rejected the key ({resp.status_code})", False
        if resp.status_code != 200:
            transient = 500 <= resp.status_code < 600 or resp.status_code == 429
            return False, f"Cal.com API returned {resp.status_code}", transient
        return True, "ok", False

    def test(
        self,
        user: User,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> tuple[bool, str]:
        """Test connectivity. If ``api_key`` is provided (non-empty), test
        those ad-hoc values — this lets the operator test a freshly-typed
        key before saving. If ``api_key`` is empty/None, test the persisted
        config (which decrypts the real key).

        Ad-hoc detection is gated on the secret field (api_key) only, not
        the plaintext base_url — the template pre-fills base_url for
        existing connections, so checking it would always trigger ad-hoc
        mode and bypass the persisted key.
        """
        if api_key:
            test_key = api_key
            test_base = base_url or ""
        else:
            conn = self.get(user)
            cfg = decrypt_config("calcom", conn.config) if conn else None
            if not cfg or not cfg.get("api_key"):
                return False, "no Cal.com API key configured"
            test_key = cfg["api_key"]
            test_base = cfg.get("base_url", "")
        ok, msg, _ = self._probe(test_key, test_base)
        return ok, msg
