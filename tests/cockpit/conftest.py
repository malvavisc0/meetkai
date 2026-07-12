"""Shared fixtures for cockpit tests: isolated in-memory DB + test app/client.

No cockpit test may ever touch the real on-disk ``data/cockpit.db`` or
``configs/cockpit/``. Both are redirected to throwaway in-memory / tmp
locations via autouse fixtures below, so individual tests do not need to
remember to patch anything.
"""

import secrets
from datetime import UTC, datetime

import pytest
from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from kai.cockpit.db import Base
from kai.cockpit.models import User


@pytest.fixture(autouse=True)
def _cockpit_env(monkeypatch):
    """Provide the env vars the cockpit modules require to construct at all."""
    monkeypatch.setenv("KAI_COCKPIT_SECRET", "test-secret")
    monkeypatch.setenv("KAI_WAHA_HMAC_KEY", "test-waha-hmac-key")
    # Tells kai.cockpit.app to skip the startup deployment-reconciliation
    # background thread — it would race the isolated in-memory SQLite
    # StaticPool connection set up by _isolated_db_engine below and corrupt
    # SQLAlchemy's identity map. Exercised directly by
    # TestReconcileDeployments in tests/cockpit/test_deployments_service.py.
    monkeypatch.setenv("KAI_COCKPIT_TESTING", "1")


@pytest.fixture(autouse=True)
def _isolated_configs_dir(tmp_path, monkeypatch):
    """Redirect config_writer.CONFIGS_DIR to a tmp dir for every cockpit test.

    ``DeploymentsService.edit()`` (and ``start()``) call
    ``config_writer.write_config()`` unconditionally, so this must be an
    autouse fixture rather than something each test remembers to patch.
    """
    from kai.cockpit import config_writer

    monkeypatch.setattr(config_writer, "CONFIGS_DIR", tmp_path / "configs" / "cockpit")


@pytest.fixture(autouse=True)
def _isolated_db_engine(monkeypatch):
    """Bind the cockpit DB module to an in-memory engine for every test.

    ``kai.cockpit.db`` creates its module-level ``engine`` /
    ``SessionLocal`` at import time, defaulting to
    ``sqlite:///data/cockpit.db`` (a production file). ``app.py`` imports
    ``create_all`` by reference at module load, so patching
    ``cockpit_db.create_all`` *after* import does not stop ``create_app()``
    from calling the real one. Patching the module-global ``engine`` (and
    the already-imported ``app.create_all``) is the only airtight way to
    guarantee no test ever creates ``data/cockpit.db``.
    """
    import kai.cockpit.app as cockpit_app
    import kai.cockpit.db as cockpit_db

    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)

    monkeypatch.setattr(cockpit_db, "engine", eng)
    monkeypatch.setattr(cockpit_db, "SessionLocal", sessionmaker(bind=eng))
    # ``app.py`` already bound ``create_all`` by reference at import time,
    # so patch the name in ``app``'s namespace too.
    monkeypatch.setattr(cockpit_app, "create_all", lambda: None)
    monkeypatch.setattr(cockpit_db, "create_all", lambda: None)
    return eng


@pytest.fixture
def engine(_isolated_db_engine):
    """In-memory SQLite engine (the autouse-isolated module global)."""
    return _isolated_db_engine


@pytest.fixture
def db(engine):
    """A DB session bound to the isolated in-memory engine."""
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def user(db):
    """Pre-created test user (synthetic — no real PII)."""
    from kai.cockpit.naming import kai_slug_for

    u = User(
        email="bob@test.com",
        language="Spanish",
        timezone="Europe/Berlin",
        hmac_key=secrets.token_hex(32),
        created_at=datetime.now(UTC).isoformat(),
        kai_slug=kai_slug_for("bob@test.com"),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _connect_whatsapp(db, user, status: str = "connected"):
    """Create (or update) a WhatsApp ``Connection`` row for ``user``.

    Plain helper, not a fixture, so tests that manage their own Connection
    rows (or specifically test the disconnected/missing-connection path)
    are never forced through it. Used by ``connected_user`` below and by
    tests/fixtures in other files that need a ready-to-go WhatsApp
    connection before calling ``DeploymentsService.create()``, which
    enforces ``BotType.required_connections``.
    """
    from kai.cockpit.models import Connection

    conn = Connection(
        user_id=user.id,
        service="whatsapp",
        status=status,
        config={
            "waha_session": f"kai-{user.kai_slug or user.email.split('@')[0]}",
            "waha_webhook_port": 8101,
            "waha_webhook_path": "/webhook/whatsapp-1",
        },
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


@pytest.fixture
def connected_user(user, db):
    """The ``user`` fixture with a connected WhatsApp ``Connection`` already
    attached — for the majority of tests that just want a "ready to go"
    user able to create a ``waha`` deployment.
    """
    _connect_whatsapp(db, user)
    return user


@pytest.fixture
def client(db, monkeypatch):
    """A Starlette TestClient wired to the isolated ``db`` session.

    Overrides the ``get_db`` FastAPI dependency so every route in the app
    uses the same in-memory session as the test (so assertions can read
    writes made by routes without a separate query).
    """
    from starlette.testclient import TestClient

    import kai.cockpit.db as cockpit_db
    from kai.cockpit.app import create_app

    app = create_app()

    def _override_get_db(request: Request):
        request.state.db = db
        yield db

    app.dependency_overrides[cockpit_db.get_db] = _override_get_db

    with TestClient(app) as c:
        yield c
