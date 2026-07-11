"""Tests for the connection catalog (Fix 02).

Covers: BotType.required/supported_connections, the catalog-driven start
gate, the supported-connection injection loop, the CREDENTIAL_TYPES
secret_fields hook, and that a tool toggle stored in settings["tools"]
is stored intent (start() skips injection when the Connection row is absent).
"""

import subprocess

import pytest

from kai.cockpit.bots import BOT_TYPES, CREDENTIAL_TYPES, BotType
from kai.cockpit.deployments import (
    ConnectionRequiredError,
    DeploymentsService,
    _inject_connection_env,
)
from kai.cockpit.models import Connection, Deployment


def _whatsapp_conn(user_id: int) -> Connection:
    return Connection(
        user_id=user_id,
        service="whatsapp",
        status="connected",
        config={
            "waha_session": "kai-bob",
            "waha_webhook_port": 8101,
            "waha_webhook_path": "/webhook/whatsapp-1",
        },
        created_at="now",
        updated_at="now",
    )


class TestCatalogData:
    def test_waha_required_connections(self):
        assert BOT_TYPES["waha"].required_connections == ["whatsapp"]

    def test_waha_supported_connections(self):
        assert BOT_TYPES["waha"].supported_connections == ["database"]

    def test_database_secret_fields(self):
        assert CREDENTIAL_TYPES["database"].secret_fields == ["url"]

    def test_smtp_secret_fields(self):
        assert CREDENTIAL_TYPES["smtp"].secret_fields == ["password"]

    def test_database_testable(self):
        assert CREDENTIAL_TYPES["database"].testable is True


class _StartBase:
    """Shared helpers for start() catalog tests."""

    @pytest.fixture(autouse=True)
    def _media_ready(self):
        from kai.cockpit.media_services import MEDIA_READY

        MEDIA_READY.set()
        yield
        MEDIA_READY.clear()

    def _run_start(self, svc, dep, monkeypatch, tmp_path, user):
        monkeypatch.setattr("kai.cockpit.config_writer.write_config", lambda d, i: None)

        class FakeProc:
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

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProc())

        from kai.config.settings import Settings
        from kai.runs import RunRecord, RunRegistry, runs_path

        fake_settings = Settings(_env_file=None, agent_history_folder=str(tmp_path))  # type: ignore[call-arg]
        monkeypatch.setattr("kai.config.settings.get_settings", lambda: fake_settings)

        instance_id = f"{dep.bot_type}-{user.email}"
        registry = RunRegistry(runs_path(fake_settings.agent_history_folder, instance_id))
        registry.replace(
            "deadbeef",
            RunRecord(
                endpoint="http://x", hmac_key="k", hmac_algorithm="sha512", pid=1, started_at="t"
            ),
        )

        svc.start(dep)


class TestStartGate(_StartBase):
    def test_rejects_when_whatsapp_missing(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        with pytest.raises(ConnectionRequiredError, match="whatsapp"):
            svc.start(dep)

    def test_rejects_when_whatsapp_not_connected(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        db.add(
            Connection(
                user_id=user.id,
                service="whatsapp",
                status="disconnected",
                config={
                    "waha_session": "kai-bob",
                    "waha_webhook_port": 8101,
                    "waha_webhook_path": "/w",
                },
                created_at="now",
                updated_at="now",
            )
        )
        db.commit()
        with pytest.raises(ConnectionRequiredError, match="whatsapp"):
            svc.start(dep)

    def test_rejects_when_second_required_missing(self, db, user, monkeypatch):
        """A bot type declaring two required connections must mention the
        missing one in the error."""
        fake_bt = BotType(
            name="multi",
            feature_flags=[],
            required_connections=["whatsapp", "email"],
            supported_connections=[],
        )
        monkeypatch.setitem(BOT_TYPES, "multi", fake_bt)

        dep = Deployment(
            user_id=user.id,
            bot_type="multi",
            run_id=None,
            status="needs_connect",
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

        # whatsapp connected, email absent → error must mention email.
        db.add(_whatsapp_conn(user.id))
        db.commit()

        svc = DeploymentsService(db)
        with pytest.raises(ConnectionRequiredError, match="email"):
            svc.start(dep)


class TestSupportedInjectionLoop(_StartBase):
    def test_not_enabled_does_not_call_injector(self, db, user, monkeypatch, tmp_path):
        """When a supported connection is not toggled on in settings["tools"],
        _inject_connection_env must not be called — start() succeeds."""
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        db.add(_whatsapp_conn(user.id))
        db.commit()

        called: list[str] = []

        def boom(_env, service, _conn):
            called.append(service)
            raise NotImplementedError(service)

        monkeypatch.setattr("kai.cockpit.deployments._inject_connection_env", boom)

        # tools not set at all → no database toggle
        self._run_start(svc, dep, monkeypatch, tmp_path, user)
        assert called == []

    def test_toggle_on_but_connection_absent_skips_injection(self, db, user, monkeypatch, tmp_path):
        """Enabling tool_database with no database Connection row is stored
        intent, not an executed grant — start() skips injection (no
        NotImplementedError)."""
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        svc.edit(dep, settings={"tools": {"database": True}})
        db.add(_whatsapp_conn(user.id))
        db.commit()

        def boom(_env, service, _conn):
            raise NotImplementedError(service)

        monkeypatch.setattr("kai.cockpit.deployments._inject_connection_env", boom)

        self._run_start(svc, dep, monkeypatch, tmp_path, user)

    def test_toggle_on_and_connection_present_calls_injector(self, db, user, monkeypatch, tmp_path):
        """When both the toggle is on and the Connection row exists, the
        injector is called — the stub raises NotImplementedError (the single
        site Fixes 05/06 fill in)."""
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        svc.edit(dep, settings={"tools": {"database": True}})
        db.add(_whatsapp_conn(user.id))
        db.add(
            Connection(
                user_id=user.id,
                service="database",
                status="connected",
                config={"label": "prod", "url": "sqlite:///x"},
                created_at="now",
                updated_at="now",
            )
        )
        db.commit()

        with pytest.raises(NotImplementedError, match="database"):
            self._run_start(svc, dep, monkeypatch, tmp_path, user)


class TestInjectConnectionEnvStub:
    def test_raises_for_unknown_service(self):
        conn = _whatsapp_conn(1)
        with pytest.raises(NotImplementedError, match="whatsapp"):
            _inject_connection_env({}, "whatsapp", conn)


class TestSettingsStoresToolToggle:
    def test_edit_stores_tools_dict(self, db, user):
        """edit() preserves settings["tools"] — the POST handler builds the
        full tools dict and passes it through. This is the stored-intent
        half: start() skipping injection when the Connection row is absent
        is covered by TestSupportedInjectionLoop above."""
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        svc.edit(dep, settings={"tools": {"database": True}})
        db.refresh(dep)
        assert dep.settings["tools"] == {"database": True}

    def test_edit_shallow_merges_not_deep(self, db, user):
        """edit() does a shallow merge: a partial settings update replaces
        the tools key entirely. The POST handler must therefore pass the
        complete tools dict, not just changed keys."""
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        svc.edit(dep, settings={"tools": {"database": True}})
        svc.edit(dep, settings={"tools": {"database": False}})
        db.refresh(dep)
        assert dep.settings["tools"] == {"database": False}
