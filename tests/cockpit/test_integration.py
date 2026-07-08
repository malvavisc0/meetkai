"""End-to-end integration test"""

import subprocess
from unittest.mock import AsyncMock

import pytest

from kai.cockpit import tokens
from kai.cockpit.auth_backends import MagicLinkProvider
from kai.cockpit.models import User
from kai.cockpit.naming import kai_slug_for


@pytest.fixture
def bob(db):
    u = User(
        email="bob@x.com",
        language="Spanish",
        timezone="Europe/Berlin",
        hmac_key="bob-hmac-key",
        created_at="now",
        is_disabled=False,
        kai_slug=kai_slug_for("bob@x.com"),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def fake_waha_client(monkeypatch):
    client = AsyncMock()
    client.close = AsyncMock()
    client.create_session.return_value = {}
    client.get_session.return_value = {"status": "WORKING"}
    monkeypatch.setattr("kai.cockpit.connections.WahaClient", lambda settings: client)
    monkeypatch.setattr("kai.cockpit.connections.get_waha_settings", lambda: object())
    return client


def _login(client, db, bob):
    """Drive the request→approve→magic-link flow and return an authenticated client."""
    tokens.create_login_request(db, bob.id)
    provider = MagicLinkProvider(db)
    token = provider.initiate_login(bob.id)
    resp = client.get(f"/auth/magic?token={token.token}", follow_redirects=False)
    assert resp.status_code == 302
    return client


class TestFullDeploymentFlow:
    @pytest.fixture(autouse=True)
    def _media_ready(self):
        """Pretend the shared STT/TTS services are up so the readiness
        gate in DeploymentsService.start() doesn't block this end-to-end test.
        """
        from kai.cockpit.media_services import MEDIA_READY

        MEDIA_READY.set()
        yield
        MEDIA_READY.clear()

    def test_end_to_end(self, client, db, bob, fake_waha_client, monkeypatch, tmp_path):
        # 5. GET / -> dashboard (no deployments)
        _login(client, db, bob)
        r = client.get("/")
        assert r.status_code == 200
        assert "waha" in r.text.lower() or "deployment" in r.text.lower()

        # 6. GET /connections -> WhatsApp disconnected
        r = client.get("/connections")
        assert r.status_code == 200

        # 7. POST /connections/whatsapp/connect -> (mock WAHA) -> connected
        r = client.post("/connections/whatsapp/connect", follow_redirects=False)
        assert r.status_code == 302

        from kai.cockpit.connections import ConnectionsService

        conn = ConnectionsService(db).get_whatsapp(bob)
        assert conn is not None
        assert conn.status == "connected"

        # 8. GET /deployments/new?bot_type=waha -> wizard
        r = client.get("/deployments/new", params={"bot_type": "waha"})
        assert r.status_code == 200

        # 9. POST /deployments/new -> deployment created, redirect to detail
        r = client.post(
            "/deployments/new",
            data={"bot_type": "waha", "goal": "be helpful", "language": "English"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/deployments/" in r.headers["location"]

        from kai.cockpit.deployments import DeploymentsService

        dep_svc = DeploymentsService(db)
        deps = dep_svc.list_for_user(bob.id)
        assert len(deps) == 1
        dep = deps[0]

        # 10. POST /deployments/{id}/start -> (mock subprocess) -> running
        monkeypatch.setattr("kai.cockpit.config_writer.write_config", lambda d, instance_id: None)

        class FakeProc:
            returncode = None

            @property
            def stdout(self):
                return self

            def __init__(self):
                self._lines = iter(["KAI_RUN_ID=cafef00d\n"])

            def readline(self):
                return next(self._lines, "")

            def poll(self):
                return self.returncode

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProc())

        import kai.cockpit.deployments as dep_mod
        from kai.config.settings import Settings
        from kai.runs import RunRecord, RunRegistry, runs_path

        fake_settings = Settings(_env_file=None, agent_history_folder=str(tmp_path))  # type: ignore[call-arg]
        monkeypatch.setattr("kai.config.settings.get_settings", lambda: fake_settings)

        instance_id = f"{dep.bot_type}-{bob.email}"
        registry = RunRegistry(runs_path(fake_settings.agent_history_folder, instance_id))
        registry.replace(
            "cafef00d",
            RunRecord(
                endpoint="http://127.0.0.1:9999",
                hmac_key="k",
                hmac_algorithm="sha512",
                pid=1,
                started_at="t",
            ),
        )

        r = client.post(f"/deployments/{dep.id}/start", follow_redirects=False)
        assert r.status_code == 302
        db.refresh(dep)
        assert dep.status == "running"
        assert dep.run_id == "cafef00d"

        # 11. GET /deployments/{id} -> status (mock /status response)
        monkeypatch.setattr(
            dep_mod.DeploymentsService,
            "fetch_status",
            lambda self, d: {"session": {"status": "WORKING"}},
        )
        r = client.get(f"/deployments/{dep.id}")
        assert r.status_code == 200

        # 12. POST /deployments/{id}/chat -> (mock /tell response) -> reply
        monkeypatch.setattr(
            dep_mod.DeploymentsService,
            "send_message",
            lambda self, d, message, persist=False: {"ok": True, "reply": "sure thing"},
        )
        r = client.post(
            f"/deployments/{dep.id}/chat", data={"message": "hello"}, follow_redirects=False
        )
        assert r.status_code == 302
        r2 = client.get(f"/deployments/{dep.id}/chat")
        assert "sure thing" in r2.text

        # 13. POST /deployments/{id}/settings -> save settings
        r = client.post(
            f"/deployments/{dep.id}/settings",
            data={"goal": "be nicer", "language": "English"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.refresh(dep)
        assert dep.goal == "be nicer"

        # The bot is running, so the detail page must render the
        # "restart to apply" button.
        r = client.get(f"/deployments/{dep.id}")
        assert r.status_code == 200
        assert "restart" in r.text.lower()

        # 14. POST /deployments/{id}/stop -> stopped
        monkeypatch.setattr("kai.cockpit.deployments.pid_alive", lambda pid: False)
        r = client.post(f"/deployments/{dep.id}/stop", follow_redirects=False)
        assert r.status_code == 302
        db.refresh(dep)
        assert dep.status == "stopped"

        # 15. POST /deployments/{id}/delete -> row gone, redirect to /
        r = client.post(
            f"/deployments/{dep.id}/delete",
            data={"confirm_delete": "true"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["location"] == "/"
        assert dep_svc.get(dep.id) is None


class TestSecondUserIsolation:
    def test_second_user_gets_separate_instance_namespace(self, db, bob):
        alice = User(
            email="alice@x.com",
            language="French",
            timezone="Europe/Paris",
            hmac_key="alice-hmac-key",
            created_at="now",
            kai_slug=kai_slug_for("alice@x.com"),
        )
        db.add(alice)
        db.commit()

        from kai.cockpit.deployments import DeploymentsService

        svc = DeploymentsService(db)
        bob_dep = svc.create(bob, "waha", "goal", "English")
        alice_dep = svc.create(alice, "waha", "goal", "French")

        bob_instance = f"{bob_dep.bot_type}-{bob.email}"
        alice_instance = f"{alice_dep.bot_type}-{alice.email}"
        assert bob_instance != alice_instance


class TestStartGatedOnWhatsApp:
    """The detail page must hide 'Start' and show 'Connect WhatsApp' when
    the Operator has no connected WhatsApp Connection."""

    def test_start_hidden_when_whatsapp_not_connected(self, client, db, bob, fake_waha_client):
        _login(client, db, bob)
        # NO WhatsApp connection created -> start must be hidden.
        from kai.cockpit.deployments import DeploymentsService

        dep = DeploymentsService(db).create(bob, "waha", "be helpful", "English")
        r = client.get(f"/deployments/{dep.id}")
        assert r.status_code == 200
        assert "Connect WhatsApp" in r.text
        assert f'/deployments/{dep.id}/start"' not in r.text

    def test_start_shown_once_whatsapp_connected(self, client, db, bob, fake_waha_client):
        _login(client, db, bob)
        # Connect WhatsApp (mocked WAHA returns WORKING immediately).
        client.post("/connections/whatsapp/connect", follow_redirects=False)

        from kai.cockpit.deployments import DeploymentsService

        dep = DeploymentsService(db).create(bob, "waha", "be helpful", "English")
        r = client.get(f"/deployments/{dep.id}")
        assert r.status_code == 200
        assert f'/deployments/{dep.id}/start"' in r.text
        assert "Connect WhatsApp" not in r.text
