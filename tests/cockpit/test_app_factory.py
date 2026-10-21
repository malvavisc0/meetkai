"""Tests for cockpit app construction and startup migration ownership."""

from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import kai.cockpit.app as app_module


def _settings(*, testing: bool, escalations_path) -> SimpleNamespace:
    return SimpleNamespace(
        cockpit_testing=testing,
        cockpit_secret="secret",
        cockpit_escalation_secret="escalation",
        cookie_secure=False,
        escalations_path=escalations_path,
    )


def _patch_app(monkeypatch, *, testing: bool, escalations_path) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        app_module,
        "get_cockpit_settings",
        lambda: _settings(testing=testing, escalations_path=escalations_path),
    )
    monkeypatch.setattr(app_module, "require_real_secret", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "init_sentry", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "run_migrations", lambda: calls.append("migrate"))
    return calls


def test_create_app_runs_migrations_when_it_owns_the_engine(monkeypatch, tmp_path):
    calls = _patch_app(monkeypatch, testing=False, escalations_path=tmp_path / "esc.json")
    engine = create_engine("sqlite://", poolclass=StaticPool)
    monkeypatch.setattr(app_module, "build_engine", lambda: engine)

    app = app_module.create_app()

    assert calls == ["migrate"]
    assert app.state.engine is engine
    engine.dispose()


def test_create_app_with_injected_factory_skips_migrations(monkeypatch, tmp_path):
    calls = _patch_app(monkeypatch, testing=False, escalations_path=tmp_path / "esc.json")
    engine = create_engine("sqlite://", poolclass=StaticPool)

    app = app_module.create_app(session_factory=sessionmaker(bind=engine))

    assert calls == []
    assert app.state.engine is None
