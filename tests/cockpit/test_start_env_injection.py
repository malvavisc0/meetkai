"""Tests for generic non-bespoke control-port + HMAC env injection (04-email-bot).

Covers: a non-bespoke bot (email) gets ``KAI_BOT_CONTROL_PORT`` /
``KAI_BOT_HMAC_KEY`` / ``KAI_BOT_CONTROL_HOST`` injected into the spawned
env, ``Deployment.settings["control_port"]`` is set on start and cleared on
stop, and ``KAI_EMAIL_VISION`` is injected when ``feature_flags["image"]``
is on. Also confirms the whatsapp (bespoke) path does NOT get the generic
``KAI_BOT_*`` env.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime

import pytest

from kai.cockpit.deployments import DeploymentsService
from kai.cockpit.models import Connection, Deployment, User
from kai.cockpit.naming import kai_slug_for

_KEY = "a" * 64


class _FakeProc:
    returncode = None

    def __init__(self):
        self._lines = iter(["starting...\n", "KAI_RUN_ID=deadbeef\n"])

    @property
    def stdout(self):
        return self

    def readline(self):
        return next(self._lines, "")

    def poll(self):
        return self.returncode


def _setup_run_registry(monkeypatch, tmp_path, instance_id: str):
    from kai.config.settings import Settings
    from kai.runs import RunRecord, RunRegistry, runs_path

    fake_settings = Settings.for_test(agent_history_folder=str(tmp_path))
    monkeypatch.setattr("kai.config.settings.get_settings", lambda: fake_settings)
    registry = RunRegistry(runs_path(fake_settings.agent_history_folder, instance_id))
    registry.replace(
        "deadbeef",
        RunRecord(
            endpoint="http://x", hmac_key="k", hmac_algorithm="sha512", pid=1, started_at="t"
        ),
    )
    return fake_settings


@pytest.fixture(autouse=True)
def _media_ready():
    from kai.cockpit.media_services import MEDIA_READY

    MEDIA_READY.set()
    yield
    MEDIA_READY.clear()


@pytest.fixture(autouse=True)
def _encryption_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAI_CREDENTIAL_ENCRYPTION_KEY", _KEY)
    monkeypatch.setenv("KAI_CREDENTIAL_KEY_VERSION", "v1")
    from kai.cockpit import secrets as secrets_mod

    secrets_mod._clear_key_cache()
    yield
    secrets_mod._clear_key_cache()


def _make_user(db) -> User:
    u = User(
        email="alice@test.com",
        language="English",
        timezone="UTC",
        hmac_key="alice-hmac-key",
        created_at=datetime.now(UTC).isoformat(),
        is_disabled=False,
        kai_slug=kai_slug_for("alice@test.com"),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _resend_conn(user_id: int) -> Connection:
    from kai.cockpit.secrets import encrypt_config

    cfg = encrypt_config("resend", {"signing_secret": "dGVzdA=="})
    return Connection(
        user_id=user_id,
        service="resend",
        status="connected",
        config=cfg,
        created_at="now",
        updated_at="now",
    )


def _smtp_conn(user_id: int) -> Connection:
    from kai.cockpit.secrets import encrypt_config

    cfg = encrypt_config("smtp", {
        "host": "smtp.example.com",
        "port": 587,
        "username": "user@example.com",
        "password": "pass",
        "from_address": "support@meetk.ai",
        "use_tls": True,
    })
    return Connection(
        user_id=user_id,
        service="smtp",
        status="connected",
        config=cfg,
        created_at="now",
        updated_at="now",
    )


def _bare_deployment(db, user, bot_type="email") -> Deployment:
    dep = Deployment(
        user_id=user.id,
        bot_type=bot_type,
        run_id=None,
        status="stopped",
        desired_state="stopped",
        voice="af_heart",
        goal="goal",
        language="English",
        feature_flags={},
        settings={},
        created_at="now",
        updated_at="now",
    )
    db.add(dep)
    db.commit()
    db.refresh(dep)
    return dep


class TestControlPortInjection:
    def test_start_injects_kai_bot_control_port(self, db, monkeypatch, tmp_path):
        user = _make_user(db)
        db.add(_resend_conn(user.id))
        db.add(_smtp_conn(user.id))
        db.commit()

        svc = DeploymentsService(db)
        dep = _bare_deployment(db, user)

        injected_env: dict = {}
        monkeypatch.setattr(subprocess, "Popen", _capture_popen_factory(injected_env))
        _setup_run_registry(monkeypatch, tmp_path, f"{dep.bot_type}-{user.email}")

        svc.start(dep)
        assert "KAI_BOT_CONTROL_PORT" in injected_env
        assert injected_env["KAI_BOT_CONTROL_HOST"] == "0.0.0.0"
        assert injected_env["KAI_BOT_HMAC_KEY"] == "alice-hmac-key"

    def test_start_stores_control_port_in_settings(self, db, monkeypatch, tmp_path):
        user = _make_user(db)
        db.add(_resend_conn(user.id))
        db.add(_smtp_conn(user.id))
        db.commit()

        svc = DeploymentsService(db)
        dep = _bare_deployment(db, user)

        monkeypatch.setattr("kai.cockpit.config_writer.write_config", lambda d, i: None)
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc())
        _setup_run_registry(monkeypatch, tmp_path, f"{dep.bot_type}-{user.email}")

        svc.start(dep)
        db.refresh(dep)
        assert dep.settings.get("control_port") is not None
        assert isinstance(dep.settings["control_port"], int)
        assert 8200 <= dep.settings["control_port"] <= 8299

    def test_stop_clears_control_port(self, db, monkeypatch, tmp_path):
        user = _make_user(db)
        db.add(_resend_conn(user.id))
        db.add(_smtp_conn(user.id))
        db.commit()

        svc = DeploymentsService(db)
        dep = _bare_deployment(db, user)

        monkeypatch.setattr("kai.cockpit.config_writer.write_config", lambda d, i: None)
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc())
        _setup_run_registry(monkeypatch, tmp_path, f"{dep.bot_type}-{user.email}")

        svc.start(dep)
        db.refresh(dep)
        assert "control_port" in dep.settings

        # Set run_id to None to exercise the early-return stop path (no real
        # process to kill — the fake Popen already exited).
        dep.run_id = None
        db.commit()
        svc.stop(dep)
        db.refresh(dep)
        assert "control_port" not in dep.settings

    def test_kai_email_vision_injected_when_image_flag_on(self, db, monkeypatch, tmp_path):
        user = _make_user(db)
        db.add(_resend_conn(user.id))
        db.add(_smtp_conn(user.id))
        db.commit()

        svc = DeploymentsService(db)
        dep = _bare_deployment(db, user)
        dep.feature_flags = {"image": True}
        db.commit()

        injected_env: dict = {}
        monkeypatch.setattr(subprocess, "Popen", _capture_popen_factory(injected_env))
        _setup_run_registry(monkeypatch, tmp_path, f"{dep.bot_type}-{user.email}")

        svc.start(dep)
        assert injected_env.get("KAI_EMAIL_VISION") == "1"

    def test_kai_email_vision_absent_when_image_flag_off(self, db, monkeypatch, tmp_path):
        user = _make_user(db)
        db.add(_resend_conn(user.id))
        db.add(_smtp_conn(user.id))
        db.commit()

        svc = DeploymentsService(db)
        dep = _bare_deployment(db, user)
        dep.feature_flags = {"image": False}
        db.commit()

        injected_env: dict = {}
        monkeypatch.setattr(subprocess, "Popen", _capture_popen_factory(injected_env))
        _setup_run_registry(monkeypatch, tmp_path, f"{dep.bot_type}-{user.email}")

        svc.start(dep)
        assert "KAI_EMAIL_VISION" not in injected_env


class TestBespokeDoesNotGetGenericEnv:
    def test_whatsapp_bot_does_not_get_kai_bot_env(self, db, monkeypatch, tmp_path):
        from tests.cockpit.conftest import _connect_whatsapp

        user = _make_user(db)
        _connect_whatsapp(db, user)

        svc = DeploymentsService(db)
        dep = _bare_deployment(db, user, bot_type="waha")

        injected_env: dict = {}
        monkeypatch.setattr(subprocess, "Popen", _capture_popen_factory(injected_env))
        _setup_run_registry(monkeypatch, tmp_path, f"{dep.bot_type}-{user.email}")

        svc.start(dep)
        assert "KAI_BOT_CONTROL_PORT" not in injected_env
        assert "KAI_BOT_HMAC_KEY" not in injected_env


def _capture_popen_factory(env_sink: dict):
    def _capture(argv, *args, **kwargs):
        env_sink.update(kwargs.get("env", {}))
        return _FakeProc()

    return _capture
