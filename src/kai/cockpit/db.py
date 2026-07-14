"""SQLAlchemy engine, session factory, and helpers for the cockpit database."""

from collections.abc import Generator

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from kai.cockpit.settings import get_cockpit_settings

db_url = get_cockpit_settings().cockpit_db

engine = create_engine(db_url, echo=False)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


def get_db(request: Request) -> Generator[Session]:
    """FastAPI dependency — yields a Session, closes on exit.

    Also stashes the session on ``request.state.db`` so plain callables that
    only receive ``request`` (e.g. the ``topbar_status`` Jinja global) can
    reuse the same request-scoped session instead of opening an extra one.
    """
    db = SessionLocal()
    request.state.db = db
    try:
        yield db
    finally:
        db.close()


def create_all():
    """Create all tables. Idempotent."""
    Base.metadata.create_all(engine)
