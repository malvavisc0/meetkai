"""Tests for kai.cockpit.deployments.DeploymentsService."""

import pytest
from tests.cockpit.helpers import _connect_email, _connect_smtp

from kai.cockpit.deployments import ConnectionRequiredError

pytestmark = pytest.mark.usefixtures("email_ready_user")


class TestReconcileDeployments:
    def test_restarts_deployment_marked_running_with_no_live_process(self, db, user, monkeypatch):
        """desired_state=='running' but no live run (e.g. after a container
        restart killed every bot subprocess) must trigger a fresh start()."""
        from kai.cockpit.deployments import DeploymentsService, reconcile_deployments

        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        # Simulate a deployment that was running before a restart: intent
        # persisted (desired_state) but no run_id / live process anymore.
        dep.desired_state = "running"
        dep.status = "stopped"
        dep.run_id = None
        db.commit()

        started: list[int] = []
        monkeypatch.setattr(
            "kai.cockpit.deployments.DeploymentsService.start",
            lambda self, d: started.append(d.id),
        )
        monkeypatch.setattr(
            "kai.cockpit.deployments.DeploymentsService.fetch_status",
            lambda self, d: None,
        )

        reconcile_deployments(db)

        assert started == [dep.id]

    def test_skips_deployment_already_alive(self, db, user, monkeypatch):
        from kai.cockpit.deployments import DeploymentsService, reconcile_deployments

        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        dep.desired_state = "running"
        db.commit()

        started: list[int] = []
        monkeypatch.setattr(
            "kai.cockpit.deployments.DeploymentsService.start",
            lambda self, d: started.append(d.id),
        )
        monkeypatch.setattr(
            "kai.cockpit.deployments.DeploymentsService.fetch_status",
            lambda self, d: {"ok": True},
        )

        reconcile_deployments(db)

        assert started == []

    def test_skips_deployment_without_connection_and_continues(self, db, user, monkeypatch):
        """A ConnectionRequiredError for one deployment must not stop the
        reconciliation of the rest."""
        import secrets
        from datetime import UTC, datetime

        from kai.cockpit.deployments import DeploymentsService, reconcile_deployments
        from kai.cockpit.models import User
        from kai.cockpit.naming import kai_slug_for

        user2 = User(
            email="alice@test.com",
            language="English",
            timezone="UTC",
            hmac_key=secrets.token_hex(32),
            created_at=datetime.now(UTC).isoformat(),
            kai_slug=kai_slug_for("alice@test.com"),
        )
        db.add(user2)
        db.commit()
        _connect_email(db, user2)
        _connect_smtp(db, user2)

        svc = DeploymentsService(db)
        dep1 = svc.create(user, "email", "goal 1", "English")
        dep1.desired_state = "running"
        dep2 = svc.create(user2, "email", "goal 2", "English")
        dep2.desired_state = "running"
        db.commit()

        started: list[int] = []

        def fake_start(self, d):
            if d.id == dep1.id:
                raise ConnectionRequiredError("no connection")
            started.append(d.id)

        monkeypatch.setattr("kai.cockpit.deployments.DeploymentsService.start", fake_start)
        monkeypatch.setattr(
            "kai.cockpit.deployments.DeploymentsService.fetch_status",
            lambda self, d: None,
        )

        reconcile_deployments(db)

        assert started == [dep2.id]

    def test_ignores_deployments_with_desired_state_stopped(self, db, user, monkeypatch):
        from kai.cockpit.deployments import DeploymentsService, reconcile_deployments

        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        assert dep.desired_state == "stopped"

        started: list[int] = []
        monkeypatch.setattr(
            "kai.cockpit.deployments.DeploymentsService.start",
            lambda self, d: started.append(d.id),
        )

        reconcile_deployments(db)

        assert started == []
