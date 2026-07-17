"""Database connection CRUD — one Connection(service="database") per operator.

The DSN is encrypted at rest; the label stays plaintext. ``save`` with an
empty ``url`` preserves the existing encrypted DSN."""

from __future__ import annotations

import logging

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from kai.cockpit.connections.probe import (
    _is_transient_db_error,
    reflect_probe_status,
)
from kai.cockpit.connections.secrets import decrypt_config, encrypt_config
from kai.cockpit.models import Connection, User
from kai.utils.common import now_iso

logger = logging.getLogger(__name__)


class DatabaseConnectionsService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, user: User) -> Connection | None:
        return (
            self.db.query(Connection)
            .filter(Connection.user_id == user.id, Connection.service == "database")
            .first()
        )

    def save(self, user: User, *, label: str, url: str) -> Connection:
        existing = self.get(user)
        if existing is None:
            config: dict = {"label": label}
            if url:
                config["url"] = url
            conn = Connection(
                user_id=user.id,
                service="database",
                status="disconnected",
                config=encrypt_config("database", config),
                created_at=now_iso(),
                updated_at=now_iso(),
            )
            self.db.add(conn)
        else:
            if url:
                existing.config = encrypt_config("database", {"label": label, "url": url})
            else:
                existing.config = {**existing.config, "label": label}
            existing.updated_at = now_iso()
            conn = existing
        self.db.commit()
        self.db.refresh(conn)

        # Probe the just-saved DSN and reflect the result in ``status``.
        # Transient failures preserve the prior status.
        test_url = decrypt_config("database", conn.config).get("url", "")
        if test_url:
            ok, _, transient = self._probe_url(test_url)
        else:
            ok, transient = False, False
        reflect_probe_status(self.db, conn, ok, transient=transient)
        return conn

    def delete(self, user: User) -> None:
        conn = self.get(user)
        if conn is not None:
            self.db.delete(conn)
            self.db.commit()

    def decrypt_url(self, user: User) -> str | None:
        conn = self.get(user)
        if conn is None:
            return None
        return decrypt_config("database", conn.config).get("url")

    def _probe_url(self, test_url: str) -> tuple[bool, str, bool]:
        """Connect to ``test_url`` and run ``SELECT 1``. Returns
        ``(ok, message, transient)`` — ``transient`` for network/timeout
        failures, False for auth rejections or invalid DSNs."""
        connect_args: dict = {}
        if not test_url.startswith("sqlite"):
            connect_args["connect_timeout"] = 5
        engine = None
        try:
            engine = create_engine(test_url, connect_args=connect_args)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True, "ok", False
        except Exception as exc:
            return False, str(exc), _is_transient_db_error(exc)
        finally:
            if engine:
                engine.dispose()

    def test(self, user: User, *, url: str | None = None) -> tuple[bool, str]:
        """Test connectivity. If ``url`` is provided (non-empty), test that
        ad-hoc value; otherwise test the persisted DSN."""
        if url:
            test_url = url
        else:
            test_url = self.decrypt_url(user)
        if not test_url:
            return False, "no connection URL configured"
        ok, msg, _ = self._probe_url(test_url)
        return ok, msg
