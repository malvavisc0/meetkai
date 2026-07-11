"""Database connection CRUD — one Connection(service="database") per operator.

The DSN (a SQLAlchemy URL) is encrypted at rest via ``encrypt_config`` /
``decrypt_config`` (the ``url`` field is listed in
``CREDENTIAL_TYPES["database"].secret_fields``). The label stays plaintext
for template rendering.

``save`` with an empty ``url`` preserves the existing encrypted DSN — the
form shows ``••••••••`` when a URL is already stored, so the operator
never re-types the DSN on a label-only edit.
"""

from __future__ import annotations

import logging

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from kai.cockpit.models import Connection, User
from kai.cockpit.secrets import decrypt_config, encrypt_config
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

    def test(self, user: User, *, url: str | None = None) -> tuple[bool, str]:
        """Test connectivity. If ``url`` is provided (non-empty), test that
        ad-hoc value — this lets the operator test a newly-typed DSN before
        saving. If ``url`` is None or empty, test the persisted DSN."""
        if url:
            test_url = url
        else:
            test_url = self.decrypt_url(user)
        if not test_url:
            return False, "no connection URL configured"
        # SQLite (in-memory or file) doesn't support connect_timeout; only
        # add it for network databases where a dead host could hang.
        connect_args: dict = {}
        if not test_url.startswith("sqlite"):
            connect_args["connect_timeout"] = 5
        engine = create_engine(test_url, connect_args=connect_args)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True, "ok"
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            return False, str(exc)
        finally:
            engine.dispose()
