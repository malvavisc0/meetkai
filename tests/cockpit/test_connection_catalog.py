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


def _resend_conn(user_id: int) -> Connection:
    return Connection(
        user_id=user_id,
        service="resend",
        status="connected",
        config={"signing_secret": "test-signing", "api_key": "test-api-key"},
        created_at="now",
        updated_at="now",
    )


def _smtp_conn(user_id: int) -> Connection:
    return Connection(
        user_id=user_id,
        service="smtp",
        status="connected",
        config={
            "host": "smtp.example.com",
            "port": 587,
            "username": "user",
            "password": "secret123",
            "from_address": "user@example.com",
            "use_tls": True,
        },
        created_at="now",
        updated_at="now",
    )


class _FakeProc:
    """Shared fake subprocess.Popen stub for the bot-start read protocol."""

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


def _capture_popen_factory(env_sink: dict):
    """Return a Popen replacement that records the env dict into ``env_sink``."""

    def _capture(argv, *args, **kwargs):
        env_sink.update(kwargs.get("env", {}))
        return _FakeProc()

    return _capture


def _setup_run_registry(monkeypatch, tmp_path, instance_id: str):
    """Register a fake run_id so start() finds it in the runs registry."""
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


class TestCatalogData:
    def test_email_required_connections(self):
        assert BOT_TYPES["email"].required_connections == ["resend", "smtp"]

    def test_email_supported_connections(self):
        assert BOT_TYPES["email"].supported_connections == ["database", "calcom"]

    def test_database_secret_fields(self):
        assert CREDENTIAL_TYPES["database"].secret_fields == ["url"]

    def test_smtp_secret_fields(self):
        assert CREDENTIAL_TYPES["smtp"].secret_fields == ["password"]

    def test_calcom_secret_fields(self):
        assert CREDENTIAL_TYPES["calcom"].secret_fields == ["api_key"]

    def test_calcom_testable(self):
        assert CREDENTIAL_TYPES["calcom"].testable is True

    def test_database_testable(self):
        assert CREDENTIAL_TYPES["database"].testable is True


class _StartBase:
    """Shared helpers for start() catalog tests."""

    def _run_start(self, svc, dep, monkeypatch, tmp_path, user):
        monkeypatch.setattr("kai.cockpit.config_writer.write_config", lambda d, i: None)

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc())

        from kai.config.settings import Settings
        from kai.runs import RunRecord, RunRegistry, runs_path

        fake_settings = Settings.for_test(agent_history_folder=str(tmp_path))
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
    def _bare_deployment(self, db, user) -> Deployment:
        """An ``email`` Deployment row constructed directly, bypassing
        ``DeploymentsService.create()`` (which now enforces
        ``required_connections`` itself — see TestCreateGate below) so
        these tests can exercise ``start()``'s own, independent check."""
        dep = Deployment(
            user_id=user.id,
            bot_type="email",
            run_id=None,
            status="stopped",
            desired_state="stopped",
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

    def test_rejects_when_resend_missing(self, db, user):
        svc = DeploymentsService(db)
        dep = self._bare_deployment(db, user)
        with pytest.raises(ConnectionRequiredError, match="resend"):
            svc.start(dep)

    def test_rejects_when_resend_not_connected(self, db, user):
        svc = DeploymentsService(db)
        dep = self._bare_deployment(db, user)
        db.add(
            Connection(
                user_id=user.id,
                service="resend",
                status="disconnected",
                config={"signing_secret": "test-signing", "api_key": "test-api-key"},
                created_at="now",
                updated_at="now",
            )
        )
        db.commit()
        with pytest.raises(ConnectionRequiredError, match="resend"):
            svc.start(dep)

    def test_rejects_when_second_required_missing(self, db, user, monkeypatch):
        """A bot type declaring two required connections must mention the
        missing one in the error."""
        fake_bt = BotType(
            name="multi",
            feature_flags=[],
            required_connections=["resend", "smtp"],
            supported_connections=[],
        )
        monkeypatch.setitem(BOT_TYPES, "multi", fake_bt)

        dep = Deployment(
            user_id=user.id,
            bot_type="multi",
            run_id=None,
            status="stopped",
            desired_state="stopped",
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

        # resend connected, smtp absent → error must mention smtp.
        db.add(_resend_conn(user.id))
        db.commit()

        svc = DeploymentsService(db)
        with pytest.raises(ConnectionRequiredError, match="smtp"):
            svc.start(dep)


class TestCreateGate:
    """DeploymentsService.create() also enforces required_connections —
    an operator can't configure a bot it will never be allowed to start."""

    def test_rejects_when_resend_missing(self, db, user):
        svc = DeploymentsService(db)
        with pytest.raises(ConnectionRequiredError, match="resend"):
            svc.create(user, "email", "goal", "English")

    def test_succeeds_once_required_connected(self, db, user):
        db.add(_resend_conn(user.id))
        db.add(_smtp_conn(user.id))
        db.commit()

        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        assert dep.id is not None


class TestSupportedInjectionLoop(_StartBase):
    def test_not_enabled_does_not_inject_supported_connection(
        self, db, user, monkeypatch, tmp_path
    ):
        """When a supported connection is not toggled on in settings["tools"],
        _inject_connection_env must not be called for it — start() succeeds.
        (Required connections like smtp are still injected.)"""
        db.add(_resend_conn(user.id))
        db.add(_smtp_conn(user.id))
        db.commit()

        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")

        called: list[str] = []

        def record(_env, service, _conn):
            called.append(service)

        monkeypatch.setattr("kai.cockpit.deployments._inject_connection_env", record)

        # tools not set at all → no database toggle
        self._run_start(svc, dep, monkeypatch, tmp_path, user)
        assert "database" not in called

    def test_toggle_on_but_connection_absent_skips_injection(self, db, user, monkeypatch, tmp_path):
        """Enabling tool_database with no database Connection row is stored
        intent, not an executed grant — start() skips injection (no
        NotImplementedError)."""
        db.add(_resend_conn(user.id))
        db.add(_smtp_conn(user.id))
        db.commit()

        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        svc.edit(dep, settings={"tools": {"database": {"enabled": True, "instruction": ""}}})

        def boom(_env, service, _conn):
            if service == "database":
                raise NotImplementedError(service)

        monkeypatch.setattr("kai.cockpit.deployments._inject_connection_env", boom)

        self._run_start(svc, dep, monkeypatch, tmp_path, user)

    def test_toggle_on_and_connection_present_injects_sql_env(
        self, db, user, monkeypatch, tmp_path
    ):
        """When both the toggle is on and the Connection row exists, the
        database DSN is injected as KAI_SQL_DSN (Fix 05 fills the stub)."""
        db.add(_resend_conn(user.id))
        db.add(_smtp_conn(user.id))
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

        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        svc.edit(dep, settings={"tools": {"database": {"enabled": True, "instruction": ""}}})

        injected_env: dict = {}

        monkeypatch.setattr(subprocess, "Popen", _capture_popen_factory(injected_env))

        _setup_run_registry(monkeypatch, tmp_path, f"{dep.bot_type}-{user.email}")

        svc.start(dep)
        assert "KAI_SQL_DSN" in injected_env
        assert injected_env["KAI_SQL_DSN"] == "sqlite:///x"
        assert injected_env["KAI_SQL_INSTRUCTION"] == ""

    def test_dict_form_injects_instruction(self, db, user, monkeypatch, tmp_path):
        """The nested-dict form injects both KAI_SQL_DSN and
        KAI_SQL_INSTRUCTION."""
        db.add(_resend_conn(user.id))
        db.add(_smtp_conn(user.id))
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

        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        svc.edit(
            dep,
            settings={"tools": {"database": {"enabled": True, "instruction": "look up orders"}}},
        )

        injected_env: dict = {}

        monkeypatch.setattr(subprocess, "Popen", _capture_popen_factory(injected_env))

        _setup_run_registry(monkeypatch, tmp_path, f"{dep.bot_type}-{user.email}")

        svc.start(dep)
        assert injected_env["KAI_SQL_DSN"] == "sqlite:///x"
        assert injected_env["KAI_SQL_INSTRUCTION"] == "look up orders"


class TestInjectConnectionEnv:
    def test_raises_for_unknown_service(self):
        conn = Connection(
            user_id=1,
            service="legacy",
            status="connected",
            config={},
            created_at="now",
            updated_at="now",
        )
        with pytest.raises(NotImplementedError, match="legacy"):
            _inject_connection_env({}, "legacy", conn)

    def test_database_injects_sql_dsn(self, monkeypatch):
        """The database branch decrypts the URL and sets KAI_SQL_DSN."""
        monkeypatch.setenv("KAI_CREDENTIAL_ENCRYPTION_KEY", "a" * 64)
        monkeypatch.setenv("KAI_CREDENTIAL_KEY_VERSION", "v1")
        from kai.cockpit.connections import secrets

        secrets._clear_key_cache()
        try:
            encrypted_url = secrets.encrypt("sqlite:///test")
            conn = Connection(
                user_id=1,
                service="database",
                status="connected",
                config={"label": "test", "url": encrypted_url},
                created_at="now",
                updated_at="now",
            )
            env: dict = {}
            _inject_connection_env(env, "database", conn)
            assert env["KAI_SQL_DSN"] == "sqlite:///test"
        finally:
            secrets._clear_key_cache()

    def test_smtp_injects_all_env_vars(self, monkeypatch):
        """The smtp branch decrypts and sets all six KAI_SMTP_TOOL_* vars."""
        monkeypatch.setenv("KAI_CREDENTIAL_ENCRYPTION_KEY", "a" * 64)
        monkeypatch.setenv("KAI_CREDENTIAL_KEY_VERSION", "v1")
        from kai.cockpit.connections import secrets

        secrets._clear_key_cache()
        try:
            encrypted_pw = secrets.encrypt("secret123")
            conn = Connection(
                user_id=1,
                service="smtp",
                status="connected",
                config={
                    "host": "smtp.example.com",
                    "port": 587,
                    "username": "user",
                    "password": encrypted_pw,
                    "from_address": "user@example.com",
                    "use_tls": True,
                },
                created_at="now",
                updated_at="now",
            )
            env: dict = {}
            injected = _inject_connection_env(env, "smtp", conn)
            assert injected is True
            assert env["KAI_SMTP_TOOL_HOST"] == "smtp.example.com"
            assert env["KAI_SMTP_TOOL_PORT"] == "587"
            assert env["KAI_SMTP_TOOL_USERNAME"] == "user"
            assert env["KAI_SMTP_TOOL_PASSWORD"] == "secret123"
            assert env["KAI_SMTP_TOOL_FROM_ADDRESS"] == "user@example.com"
            assert env["KAI_SMTP_TOOL_USE_TLS"] == "true"
        finally:
            secrets._clear_key_cache()


class TestSettingsStoresToolToggle:
    def test_edit_stores_tools_dict(self, db, user):
        """edit() preserves settings["tools"] — the POST handler builds the
        full tools dict and passes it through. This is the stored-intent
        half: start() skipping injection when the Connection row is absent
        is covered by TestSupportedInjectionLoop above."""
        db.add(_resend_conn(user.id))
        db.add(_smtp_conn(user.id))
        db.commit()

        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        svc.edit(dep, settings={"tools": {"database": {"enabled": True, "instruction": ""}}})
        db.refresh(dep)
        assert dep.settings["tools"] == {"database": {"enabled": True, "instruction": ""}}

    def test_edit_shallow_merges_not_deep(self, db, user):
        """edit() does a shallow merge: a partial settings update replaces
        the tools key entirely. The POST handler must therefore pass the
        complete tools dict, not just changed keys."""
        db.add(_resend_conn(user.id))
        db.add(_smtp_conn(user.id))
        db.commit()

        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        svc.edit(dep, settings={"tools": {"database": {"enabled": True, "instruction": ""}}})
        svc.edit(dep, settings={"tools": {"database": {"enabled": False, "instruction": ""}}})
        db.refresh(dep)
        assert dep.settings["tools"] == {"database": {"enabled": False, "instruction": ""}}
