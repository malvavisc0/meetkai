"""SQLAlchemy engine, session factory, and helpers for the cockpit database."""

import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

db_url = os.environ.get("KAI_COCKPIT_DB", "sqlite:///data/cockpit.db")

engine = create_engine(db_url, echo=False)
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
    """Create all tables. Idempotent."""
    Base.metadata.create_all(engine)
