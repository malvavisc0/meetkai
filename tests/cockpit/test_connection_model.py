"""Tests for the ingress-only connection data model (01-connection-model).

Covers: the ``WebhookConnectionType`` registry, ``CONNECTION_LABELS`` spread
from both registries, ``encrypt_config``/``decrypt_config`` accepting resend
(error-message change lives in test_secrets.py), ``is_connected``'s
ingress-only predicate, ``_inject_connection_env``'s early-return no-op for
ingress-only services, and the required-credential env-injection loop in
``start()`` (the email bot's required smtp → ``KAI_SMTP_TOOL_*``).
"""

import subprocess

import pytest

from kai.cockpit.bots import (
    BOT_TYPES,
    CONNECTION_LABELS,
    CREDENTIAL_TYPES,
    WEBHOOK_CONNECTION_TYPES,
    BotType,
)
from kai.cockpit.deployments import (
    ConnectionRequiredError,
    DeploymentsService,
    _inject_connection_env,
    is_connected,
)
from kai.cockpit.models import Connection, Deployment

_KEY = "a" * 64


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


class _StartBase:
    """Shared helpers for start() tests."""

    @pytest.fixture(autouse=True)
    def _media_ready(self):
        from kai.cockpit.media_services import MEDIA_READY

        MEDIA_READY.set()
        yield
        MEDIA_READY.clear()


class TestWebhookConnectionCatalog:
    def test_resend_registered(self):
        assert "resend" in WEBHOOK_CONNECTION_TYPES

    def test_resend_service_and_webhook_type(self):
        wt = WEBHOOK_CONNECTION_TYPES["resend"]
        assert wt.service == "resend"
        assert wt.webhook_type == "resend"

    def test_resend_label(self):
        assert WEBHOOK_CONNECTION_TYPES["resend"].label == "Email Inbox (Resend)"

    def test_resend_secret_fields(self):
        assert WEBHOOK_CONNECTION_TYPES["resend"].secret_fields == ["signing_secret", "api_key"]

    def test_resend_testable_default_true(self):
        # mirrors CredentialType so the existing "Test connection" affordance renders
        assert WEBHOOK_CONNECTION_TYPES["resend"].testable is True

    def test_resend_fields_reuse_credential_field_shape(self):
        wt = WEBHOOK_CONNECTION_TYPES["resend"]
        names = [f.name for f in wt.fields]
        assert names == ["signing_secret", "api_key"]
        assert wt.fields[0].type == "secret"
        assert wt.fields[0].required is True
        assert wt.fields[1].type == "secret"
        assert wt.fields[1].required is True

    def test_webhook_connection_type_testable_has_default(self):
        # testable defaults to True (the resend connection has a self-loopback test),
        # unlike CredentialType whose default is False.
        from kai.cockpit.bots import WebhookConnectionType

        wt = WebhookConnectionType(service="x", label="x", fields=[], webhook_type="x")
        assert wt.testable is True

    def test_connection_labels_spreads_both_registries(self):
        # whatsapp is its own entry; credential + webhook services spread from
        # their registries — a second ingress-only provider would appear here
        # by adding a WEBHOOK_CONNECTION_TYPES entry, no extra plumbing.
        assert CONNECTION_LABELS["whatsapp"] == "WhatsApp"
        for service, ct in CREDENTIAL_TYPES.items():
            assert CONNECTION_LABELS[service] == ct.label
        for service, wt in WEBHOOK_CONNECTION_TYPES.items():
            assert CONNECTION_LABELS[service] == wt.label

    def test_resend_not_in_credential_types(self):
        # resend is an ingress-only connection, not a credential one — it must
        # not leak into CREDENTIAL_TYPES (which drives env injection / save
        # services) or it would be treated as a credential by the old code.
        assert "resend" not in CREDENTIAL_TYPES


class TestIsConnected:
    def _conn(self, service, status, config=None):
        return Connection(
            user_id=1,
            service=service,
            status=status,
            config=config or {},
            created_at="now",
            updated_at="now",
        )

    def test_none_is_not_connected(self):
        assert is_connected("resend", None) is False

    def test_bespoke_uses_status_only(self):
        # whatsapp is not in WEBHOOK_CONNECTION_TYPES → existing semantics
        assert is_connected("whatsapp", self._conn("whatsapp", "connected")) is True
        assert is_connected("whatsapp", self._conn("whatsapp", "disconnected")) is False

    def test_credential_uses_status_only(self):
        assert is_connected("database", self._conn("database", "connected")) is True
        assert is_connected("smtp", self._conn("smtp", "disconnected")) is False

    def test_ingress_connected_when_status_and_secret(self):
        c = self._conn(
            "resend", "connected", {"signing_secret": "whsec_live_x", "api_key": "re_live_x"}
        )
        assert is_connected("resend", c) is True

    def test_ingress_not_connected_when_api_key_missing(self):
        # signing_secret alone is not enough -- api_key is required to fetch
        # email content (the webhook itself carries none).
        c = self._conn("resend", "connected", {"signing_secret": "whsec_live_x"})
        assert is_connected("resend", c) is False

    def test_ingress_not_connected_when_secret_empty(self):
        # an ingress row with an empty secret must not be treated as connected,
        # or a bot could start with no way to verify its inbound webhooks.
        c = self._conn("resend", "connected", {"signing_secret": ""})
        assert is_connected("resend", c) is False

    def test_ingress_not_connected_when_secret_missing(self):
        c = self._conn("resend", "connected", {})
        assert is_connected("resend", c) is False

    def test_ingress_not_connected_when_status_disconnected(self):
        c = self._conn("resend", "disconnected", {"signing_secret": "whsec_live_x"})
        assert is_connected("resend", c) is False


class TestInjectConnectionEnvIngressNoOp:
    def test_resend_is_no_op(self):
        # ingress-only connections inject nothing — the bot receives events
        # via /ingest, not env vars, and the cockpit verifies at ingress.
        conn = Connection(
            user_id=1,
            service="resend",
            status="connected",
            config={"signing_secret": "whsec_live_x"},
            created_at="now",
            updated_at="now",
        )
        env: dict = {}
        injected = _inject_connection_env(env, "resend", conn)
        assert injected is False
        assert env == {}

    def test_resend_no_op_does_not_leak_signing_secret(self):
        # the signing secret must never reach the subprocess env — the bot
        # doesn't verify webhooks (the cockpit does) and leaking it would be
        # a security mistake.
        conn = Connection(
            user_id=1,
            service="resend",
            status="connected",
            config={"signing_secret": "top-secret-value"},
            created_at="now",
            updated_at="now",
        )
        env: dict = {}
        _inject_connection_env(env, "resend", conn)
        assert "signing_secret" not in env
        assert "top-secret-value" not in str(env.values())


class TestStartIngestsRequiredSmtp(_StartBase):
    """The email bot declares ``smtp`` in ``required_connections``. The
    supported-connections loop skips required connections and the bespoke
    block is whatsapp-only, so without the required-credential env-injection
    loop the bot would start with no ``KAI_SMTP_TOOL_*`` → no ``send_email``
    tool and no reply path."""

    @pytest.fixture(autouse=True)
    def _encryption_env(self, monkeypatch, tmp_path):
        # _inject_connection_env → decrypt_config reads .env from CWD; chdir
        # to tmp so the production .env never leaks the real key version.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("KAI_CREDENTIAL_ENCRYPTION_KEY", _KEY)
        monkeypatch.setenv("KAI_CREDENTIAL_KEY_VERSION", "v1")
        from kai.cockpit.connections import secrets as secrets_mod

        secrets_mod._clear_key_cache()
        yield
        secrets_mod._clear_key_cache()

    def _smtp_bot(self, monkeypatch):
        fake_bt = BotType(
            name="email",
            feature_flags=[],
            required_connections=["smtp"],
            supported_connections=[],
        )
        monkeypatch.setitem(BOT_TYPES, "email", fake_bt)

    def _smtp_conn(self, user_id):
        from kai.cockpit.connections import secrets

        encrypted_pw = secrets.encrypt("secret123")
        return Connection(
            user_id=user_id,
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

    def _bare_deployment(self, db, user) -> Deployment:
        dep = Deployment(
            user_id=user.id,
            bot_type="email",
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

    def test_start_injects_required_smtp(self, db, user, monkeypatch, tmp_path):
        self._smtp_bot(monkeypatch)
        db.add(self._smtp_conn(user.id))
        db.commit()

        svc = DeploymentsService(db)
        dep = self._bare_deployment(db, user)

        injected_env: dict = {}
        monkeypatch.setattr(subprocess, "Popen", _capture_popen_factory(injected_env))
        _setup_run_registry(monkeypatch, tmp_path, f"{dep.bot_type}-{user.email}")

        svc.start(dep)
        assert "KAI_SMTP_TOOL_HOST" in injected_env
        assert injected_env["KAI_SMTP_TOOL_HOST"] == "smtp.example.com"
        assert injected_env["KAI_SMTP_TOOL_PASSWORD"] == "secret123"
        assert injected_env["KAI_SMTP_TOOL_USE_TLS"] == "true"

    def test_start_rejects_when_required_smtp_disconnected(self, db, user, monkeypatch):
        self._smtp_bot(monkeypatch)
        db.add(
            Connection(
                user_id=user.id,
                service="smtp",
                status="disconnected",
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
        )
        db.commit()

        svc = DeploymentsService(db)
        dep = self._bare_deployment(db, user)
        with pytest.raises(ConnectionRequiredError, match="smtp"):
            svc.start(dep)


class TestStartIngestsRequiredResend(_StartBase):
    """A bot requiring a resend (ingress-only) connection must also pass the
    start gate via ``is_connected`` (secret non-empty + connected), and the
    required-credential loop must no-op it (no env, no crash)."""

    def _resend_bot(self, monkeypatch):
        fake_bt = BotType(
            name="email",
            feature_flags=[],
            required_connections=["resend"],
            supported_connections=[],
        )
        monkeypatch.setitem(BOT_TYPES, "email", fake_bt)

    def _bare_deployment(self, db, user) -> Deployment:
        dep = Deployment(
            user_id=user.id,
            bot_type="email",
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

    def test_start_succeeds_and_noops_resend(self, db, user, monkeypatch, tmp_path):
        self._resend_bot(monkeypatch)
        db.add(
            Connection(
                user_id=user.id,
                service="resend",
                status="connected",
                config={"signing_secret": "whsec_live_x", "api_key": "re_live_x"},
                created_at="now",
                updated_at="now",
            )
        )
        db.commit()

        svc = DeploymentsService(db)
        dep = self._bare_deployment(db, user)

        injected_env: dict = {}
        monkeypatch.setattr(subprocess, "Popen", _capture_popen_factory(injected_env))
        _setup_run_registry(monkeypatch, tmp_path, f"{dep.bot_type}-{user.email}")

        svc.start(dep)
        # resend is ingress-only: nothing injected, no secrets leaked
        assert all(
            not v.endswith("whsec_live_x") and not v.endswith("re_live_x")
            if isinstance(v, str)
            else True
            for v in injected_env.values()
        )
        assert "signing_secret" not in injected_env
        assert "api_key" not in injected_env

    def test_start_rejects_resend_when_secret_empty(self, db, user, monkeypatch):
        self._resend_bot(monkeypatch)
        db.add(
            Connection(
                user_id=user.id,
                service="resend",
                status="connected",
                config={"signing_secret": ""},
                created_at="now",
                updated_at="now",
            )
        )
        db.commit()

        svc = DeploymentsService(db)
        dep = self._bare_deployment(db, user)
        with pytest.raises(ConnectionRequiredError, match="resend"):
            svc.start(dep)


class TestCreateGateUsesIsConnected:
    """create() must reject an ingress-only required connection whose secret
    is empty (``is_connected`` returns False for it), not just one that's
    missing entirely."""

    def _resend_bot(self, monkeypatch):
        fake_bt = BotType(
            name="email",
            feature_flags=[],
            required_connections=["resend"],
            supported_connections=[],
        )
        monkeypatch.setitem(BOT_TYPES, "email", fake_bt)

    def test_create_rejects_resend_when_secret_empty(self, db, user, monkeypatch):
        self._resend_bot(monkeypatch)
        db.add(
            Connection(
                user_id=user.id,
                service="resend",
                status="connected",
                config={"signing_secret": ""},
                created_at="now",
                updated_at="now",
            )
        )
        db.commit()

        svc = DeploymentsService(db)
        with pytest.raises(ConnectionRequiredError, match="resend"):
            svc.create(user, "email", "goal", "English")

    def test_create_succeeds_when_resend_connected_with_secret(self, db, user, monkeypatch):
        self._resend_bot(monkeypatch)
        db.add(
            Connection(
                user_id=user.id,
                service="resend",
                status="connected",
                config={"signing_secret": "whsec_live_x", "api_key": "re_live_x"},
                created_at="now",
                updated_at="now",
            )
        )
        db.commit()

        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        assert dep.id is not None
