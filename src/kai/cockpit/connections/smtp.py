"""SMTP connection CRUD — one Connection(service="smtp") per operator.

The password is encrypted at rest; all other fields stay plaintext.
``save`` with an empty ``password`` preserves the existing password.
Distinct from ``cockpit/mailer.py``'s ``KAI_SMTP_*`` (the cockpit's login-link relay).
"""

from __future__ import annotations

import logging
import smtplib

from sqlalchemy.orm import Session

from kai.cockpit.connections.probe import reflect_probe_status, run_smtp_probe_with_timeout
from kai.cockpit.connections.secrets import decrypt_config, encrypt_config
from kai.cockpit.models import Connection, User
from kai.utils.common import now_iso

logger = logging.getLogger(__name__)


def _smtp_test(
    host: str, port: int, username: str, password: str, use_tls: bool
) -> tuple[bool, str]:
    """Connect + auth + NOOP against an SMTP server.

    Raises on failure so callers can inspect the exception type to classify
    transient vs auth errors.
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

        # Probe the just-saved credentials and reflect the result in ``status``.
        # Transient failures preserve the prior status.
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
        those ad-hoc values; otherwise test the persisted config.
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
