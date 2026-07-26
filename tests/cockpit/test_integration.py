"""End-to-end integration test"""

import subprocess

import pytest
from tests.cockpit.helpers import _connect_email, _connect_smtp, csrf_post

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


def _login(client, db, bob):
    """Drive the request→approve→magic-link flow and return an authenticated client."""
    tokens.create_login_request(db, bob.id)
    provider = MagicLinkProvider(db)
    token = provider.initiate_login(bob.id)
    resp = client.get(f"/login/auth?token={token.token}", follow_redirects=False)
    assert resp.status_code == 302
    return client


class TestFullDeploymentFlow:
    def test_end_to_end(self, client, db, bob, monkeypatch, tmp_path):
        # 5. GET /console -> console (no deployments)
        _login(client, db, bob)
        r = client.get("/console")
        assert r.status_code == 200
        assert "deployment" in r.text.lower()

        # Connect the email bot's required connections (resend + smtp).
        _connect_email(db, bob)
        _connect_smtp(db, bob)

        # 8. GET /deployments/new?bot_type=email -> wizard
        r = client.get("/deployments/new", params={"bot_type": "email"})
        assert r.status_code == 200

        # 9. POST /deployments/new -> deployment created, redirect to detail
        r = csrf_post(
            client,
            "/deployments/new",
            data={"bot_type": "email", "goal": "be helpful", "language": "English"},
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

        fake_settings = Settings.for_test(agent_history_folder=str(tmp_path))
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

        r = csrf_post(client, f"/deployments/{dep.id}/start", follow_redirects=False)
        assert r.status_code == 302
        db.refresh(dep)
        assert dep.status == "running"
        assert dep.run_id == "cafef00d"

        # 11. GET /deployments/{id} -> status (mock /status response)
        monkeypatch.setattr(
            dep_mod.DeploymentsService,
            "fetch_status",
            lambda self, d: {"ok": True},
        )
        r = client.get(f"/deployments/{dep.id}")
        assert r.status_code == 200

        # 12. POST /deployments/{id}/chat -> (mock /tell response) -> reply
        monkeypatch.setattr(
            dep_mod.DeploymentsService,
            "send_message",
            lambda self, d, message, persist=False: {"ok": True, "reply": "sure thing"},
        )
        r = csrf_post(
            client, f"/deployments/{dep.id}/chat", data={"message": "hello"}, follow_redirects=False
        )
        assert r.status_code == 302
        r2 = client.get(f"/deployments/{dep.id}")
        assert "sure thing" in r2.text

        # 13. POST /deployments/{id}/settings -> save settings
        r = csrf_post(
            client,
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
        r = csrf_post(client, f"/deployments/{dep.id}/stop", follow_redirects=False)
        assert r.status_code == 302
        db.refresh(dep)
        assert dep.status == "stopped"

        # 15. POST /deployments/{id}/delete -> row gone, redirect to /console
        r = csrf_post(
            client,
            f"/deployments/{dep.id}/delete",
            data={"confirm_delete": "true"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["location"] == "/console"
        assert dep_svc.get(dep.id) is None
