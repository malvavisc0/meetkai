"""SQLAlchemy engine, session factory, and helpers for the cockpit database."""

import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

_db_url = os.environ.get("KAI_COCKPIT_DB", "sqlite:///data/cockpit.db")

engine = create_engine(_db_url, echo=False)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session]:
    """FastAPI dependency — yields a Session, closes on exit."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_all():
    """Create all tables. Idempotent.

    Also backfills the ``connections.webhook_port`` column (and its unique
    index) for databases created before that column existed, since we have
    no migration framework and ``create_all`` only creates missing tables.
    """
    Base.metadata.create_all(engine)
    _ensure_webhook_port_column()


def _ensure_webhook_port_column() -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "connections" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("connections")}
    if "webhook_port" in columns:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE connections ADD COLUMN webhook_port INTEGER"))
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_connections_webhook_port "
                "ON connections (webhook_port)"
            )
        )
