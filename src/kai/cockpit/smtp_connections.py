"""SMTP connection CRUD — one Connection(service="smtp") per operator.

The password is encrypted at rest via ``encrypt_config`` /
``decrypt_config`` (the ``password`` field is listed in
``CREDENTIAL_TYPES["smtp"].secret_fields``). All other fields (host, port,
username, from_address, use_tls) stay plaintext for template rendering.

``save`` with an empty ``password`` preserves the existing encrypted
password — the form shows ``••••••••`` when a password is already stored,
so the operator never re-types the password on a host-only edit.

Distinct from ``cockpit/mailer.py``'s ``KAI_SMTP_*`` (the cockpit's own
login-link relay).
"""

from __future__ import annotations

import logging
import smtplib

from sqlalchemy.orm import Session

from kai.cockpit.connection_probe import reflect_probe_status, run_smtp_probe_with_timeout
from kai.cockpit.models import Connection, User
from kai.cockpit.secrets import decrypt_config, encrypt_config
from kai.utils.common import now_iso

logger = logging.getLogger(__name__)


def _smtp_test(
    host: str, port: int, username: str, password: str, use_tls: bool
) -> tuple[bool, str]:
    """Connect + auth + NOOP against an SMTP server.

    Raises on failure (does NOT catch exceptions) so callers can inspect
    the exception type to classify transient vs auth errors. The operator-
    facing ``test()`` wraps this in try/except to produce ``(False, msg)``
    for the flash message; ``run_smtp_probe_with_timeout`` catches to
    classify the failure mode.
    """
    with smtplib.SMTP(host, int(port), timeout=10) as server:
        server.ehlo()
        if use_tls:
            if not server.has_extn("starttls"):
                return False, "server does not support STARTTLS"
            server.starttls()
            server.ehlo()
        if username and password:
            server.login(username, password)
        server.noop()
    return True, "ok"


class SmtpConnectionsService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, user: User) -> Connection | None:
        return (
            self.db.query(Connection)
            .filter(Connection.user_id == user.id, Connection.service == "smtp")
            .first()
        )

    def save(
        self,
        user: User,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        from_address: str,
        use_tls: bool = True,
    ) -> Connection:
        existing = self.get(user)
        config: dict = {
            "host": host,
            "port": int(port),
            "username": username,
            "from_address": from_address,
            "use_tls": bool(use_tls),
        }
        if password:
            config["password"] = password
        encrypted = encrypt_config("smtp", config)
        if existing is None:
            conn = Connection(
                user_id=user.id,
                service="smtp",
                status="disconnected",
                config=encrypted,
                created_at=now_iso(),
                updated_at=now_iso(),
            )
            self.db.add(conn)
        else:
            if not password:
                # Preserve existing encrypted password; merge other fields.
                existing.config = {
                    **existing.config,
                    **{k: v for k, v in encrypted.items() if k != "password"},
                }
            else:
                existing.config = encrypted
            existing.updated_at = now_iso()
            conn = existing
        self.db.commit()
        self.db.refresh(conn)

        # Probe the just-saved credentials and reflect the result in
        # ``status``. Transient failures (network/timeout) preserve the
        # prior status so a blip doesn't block deploys; only auth
        # rejections mark the connection ``disconnected``. Decrypts the
        # just-persisted config (avoids a redundant SELECT from
        # ``self.test(user)``).
        cfg = decrypt_config("smtp", conn.config)
        ok, _, transient = run_smtp_probe_with_timeout(
            cfg.get("host", ""),
            int(cfg.get("port", 0)),
            cfg.get("username", ""),
            cfg.get("password", ""),
            cfg.get("use_tls", True),
        )
        reflect_probe_status(self.db, conn, ok, transient=transient)
        return conn

    def delete(self, user: User) -> None:
        conn = self.get(user)
        if conn is not None:
            self.db.delete(conn)
            self.db.commit()

    def decrypt_config(self, user: User) -> dict | None:
        conn = self.get(user)
        if conn is None:
            return None
        return decrypt_config("smtp", conn.config)

    def test(
        self,
        user: User,
        *,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool | None = None,
    ) -> tuple[bool, str]:
        """Test connectivity. If ``password`` is provided (non-empty), test
        those ad-hoc values — this lets the operator test a freshly-typed
        config before saving. If ``password`` is empty/None, test the
        persisted config (which decrypts the real password).

        Ad-hoc detection is gated on the secret field (password) only, not
        the plaintext fields — the template pre-fills host/port/username
        for existing connections, so checking those would always trigger
        ad-hoc mode and bypass the persisted password.
        """
        if password:
            test_host = host or ""
            test_port = int(port) if port else 0
            test_user = username or ""
            test_pass = password
            test_tls = use_tls if use_tls is not None else True
        else:
            cfg = self.decrypt_config(user)
            if not cfg or not cfg.get("host"):
                return False, "no SMTP host configured"
            test_host = cfg["host"]
            test_port = int(cfg.get("port", 0))
            test_user = cfg.get("username", "")
            test_pass = cfg.get("password", "")
            test_tls = cfg.get("use_tls", True)
        if not test_host:
            return False, "no SMTP host configured"
        try:
            return _smtp_test(test_host, test_port, test_user, test_pass, test_tls)
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            return False, str(exc)
