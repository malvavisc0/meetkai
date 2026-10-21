"""SQLAlchemy engine, session factory, and helpers for the cockpit database."""

from collections.abc import Generator
from functools import lru_cache
from pathlib import Path

from fastapi import Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from kai.cockpit.settings import get_cockpit_settings

SessionFactory = sessionmaker[Session]


class Base(DeclarativeBase):
    """Declarative base for cockpit ORM models."""

    pass


def build_engine() -> Engine:
    """Create the engine for the configured cockpit database."""
    return create_engine(get_cockpit_settings().cockpit_db, echo=False)


def build_session_factory(engine: Engine) -> SessionFactory:
    """Bind a session factory to an engine."""
    return sessionmaker(bind=engine)


@lru_cache(maxsize=1)
def default_session_factory() -> SessionFactory:
    """Return the process-wide session factory for standalone callers."""
    return build_session_factory(build_engine())


def open_session() -> Session:
    """Open a standalone session against the configured cockpit database."""
    return default_session_factory()()


def get_db(request: Request) -> Generator[Session]:
    """FastAPI dependency — yields a Session from the app's factory, closes on exit.

    Also stashes the session on ``request.state.db`` so plain callables that
    only receive ``request`` (e.g. the ``topbar_status`` Jinja global) can
    reuse the same request-scoped session instead of opening an extra one.
    """
    db = request.app.state.session_factory()
    request.state.db = db
    try:
        yield db
    finally:
        db.close()


def run_migrations() -> None:
    """Apply Alembic migrations to the configured DB (``alembic upgrade head``).

    Reads the DB URL from ``CockpitSettings.cockpit_db`` so the same code
    path works for SQLite today and MySQL tomorrow (the Alembic URL is
    never hardcoded — the future MySQL switch needs no Alembic change).
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(alembic_ini_path()))
    cfg.set_main_option("sqlalchemy.url", get_cockpit_settings().cockpit_db)
    command.upgrade(cfg, "head")


def alembic_ini_path() -> Path:
    """Locate ``alembic.ini`` regardless of the current working directory.

    Prefers the CWD copy (dev checkouts run from the repo root), then falls
    back to the repo root resolved from this file's location (packaged or
    container installs). Raises when neither candidate exists."""
    cwd = Path("alembic.ini")
    if cwd.is_file():
        return cwd
    repo_root = Path(__file__).resolve().parents[3] / "alembic.ini"
    if not repo_root.is_file():
        raise FileNotFoundError("alembic.ini not found in the CWD or the repo root")
    return repo_root
