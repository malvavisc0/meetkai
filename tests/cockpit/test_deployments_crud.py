"""Tests for kai.cockpit.deployments.DeploymentsService."""

import pytest

from kai.cockpit.deployments import (
    ConnectionRequiredError,
    DeploymentsService,
)
from kai.cockpit.models import Connection

pytestmark = pytest.mark.usefixtures("email_ready_user")


class TestCreate:
    def test_create_happy_path(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "be helpful", "English")
        assert dep.id is not None
        assert dep.status == "stopped"
        assert dep.desired_state == "stopped"
        assert dep.settings["language"] == "English"
        assert dep.feature_flags == {"image": False}

    def test_create_seeds_email_schema(self, db, user):
        """Regression: ``create()`` seeds the deployment from the email
        BotConfig (``vision``, no ``media`` block), not some stale
        transport's schema."""
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "answer support emails", "English")
        assert "media" not in dep.settings
        assert "vision" in dep.settings
        assert dep.settings["language"] == "English"

    def test_reject_empty_goal(self, db, user):
        svc = DeploymentsService(db)
        with pytest.raises(ValueError):
            svc.create(user, "email", "", "English")

    def test_reject_empty_language(self, db, user):
        svc = DeploymentsService(db)
        with pytest.raises(ValueError):
            svc.create(user, "email", "goal", "")

    def test_reject_disabled_user(self, db, user):
        user.is_disabled = True
        db.commit()
        svc = DeploymentsService(db)
        with pytest.raises(ValueError):
            svc.create(user, "email", "goal", "English")

    def test_reject_unknown_bot_type(self, db, user):
        svc = DeploymentsService(db)
        with pytest.raises(ValueError):
            svc.create(user, "telegram", "goal", "English")

    def test_unique_constraint_per_user_bot_type(self, db, user):
        svc = DeploymentsService(db)
        svc.create(user, "email", "goal", "English")
        with pytest.raises(ValueError):
            svc.create(user, "email", "another goal", "English")


class TestCreateConnectionGate:
    """DeploymentsService.create() enforces BotType.required_connections —
    a bot can't even be created before its required connections exist, not
    just started (see also TestConnectionCatalog's start()-time gate)."""

    def test_rejects_when_resend_missing(self, db):
        """A user fixture with no Connection row at all (bypassing the
        module's ``email_ready_user`` fixture by constructing a
        fresh user) must be rejected at create()."""
        import secrets
        from datetime import UTC, datetime

        from kai.cockpit.models import User
        from kai.cockpit.naming import kai_slug_for

        lonely_user = User(
            email="lonely@test.com",
            language="English",
            timezone="UTC",
            hmac_key=secrets.token_hex(32),
            created_at=datetime.now(UTC).isoformat(),
            kai_slug=kai_slug_for("lonely@test.com"),
        )
        db.add(lonely_user)
        db.commit()

        svc = DeploymentsService(db)
        with pytest.raises(ConnectionRequiredError, match="resend"):
            svc.create(lonely_user, "email", "goal", "English")

    def test_rejects_when_resend_disconnected(self, db, user):
        conn = db.query(Connection).filter_by(user_id=user.id, service="resend").first()
        conn.status = "disconnected"
        db.commit()

        svc = DeploymentsService(db)
        with pytest.raises(ConnectionRequiredError, match="resend"):
            svc.create(user, "email", "goal", "English")

    def test_succeeds_once_email_connected(self, db, user):
        # The email_ready_user fixture already connected resend + smtp.
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        assert dep.id is not None


class TestEdit:
    def test_edit_updates_goal(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        svc.edit(dep, goal="new goal")
        assert dep.goal == "new goal"

    def test_edit_rejects_empty_goal(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        with pytest.raises(ValueError):
            svc.edit(dep, goal="")

    def test_edit_feature_flags_validates_subset(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        with pytest.raises(ValueError):
            svc.edit(dep, feature_flags={"telegram": True})

    def test_edit_feature_flags_happy_path(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        svc.edit(dep, feature_flags={"image": False})
        assert dep.feature_flags == {"image": False}

    def test_edit_settings_merges_partial(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        original_temperature = dep.settings["temperature"]
        svc.edit(dep, settings={"blacklist": ["a@b.c"]})
        assert dep.settings["blacklist"] == ["a@b.c"]
        assert dep.settings["temperature"] == original_temperature


class TestGetAndList:
    def test_get_returns_existing_deployment(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        fetched = svc.get(dep.id)
        assert fetched is not None
        assert fetched.id == dep.id

    def test_get_missing_returns_none(self, db, user):
        svc = DeploymentsService(db)
        assert svc.get(999) is None

    def test_list_for_user(self, db, user):
        svc = DeploymentsService(db)
        svc.create(user, "email", "goal", "English")
        assert len(svc.list_for_user(user.id)) == 1

    def test_resolve_run_caches_registry_lookup(self, db, user, monkeypatch):
        from unittest.mock import MagicMock

        from kai.runs import RunRecord

        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        dep.run_id = "run-1"
        record = RunRecord(
            run_id="run-1",
            endpoint="http://x",
            hmac_key="k",
            hmac_algorithm="sha512",
            pid=1,
            pid_start=1,
            started_at="t",
        )
        registry = MagicMock()
        registry.get.return_value = record
        monkeypatch.setattr(svc, "run_registry", lambda deployment, user=None: registry)

        assert svc.resolve_run(dep) is record
        assert svc.resolve_run(dep) is record
        registry.get.assert_called_once()

    def test_resolve_run_cache_cleared_on_stop(self, db, user, monkeypatch):
        from unittest.mock import MagicMock

        from kai.cockpit import deployment_process
        from kai.runs import RunRecord

        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        dep.run_id = "run-1"
        record = RunRecord(
            run_id="run-1",
            endpoint="http://x",
            hmac_key="k",
            hmac_algorithm="sha512",
            pid=1,
            pid_start=1,
            started_at="t",
        )
        registry = MagicMock()
        registry.get.side_effect = [record, None]
        monkeypatch.setattr(svc, "run_registry", lambda deployment, user=None: registry)
        monkeypatch.setattr(deployment_process, "stop", lambda *args, **kwargs: None)

        assert svc.resolve_run(dep) is record
        svc.stop(dep)
        assert svc.resolve_run(dep) is None
        assert registry.get.call_count == 2

    def test_interaction_summary_counts_both_history_layouts(self, db, user, tmp_path, monkeypatch):
        import json

        from kai.config.settings import Settings

        settings = Settings.for_test(agent_history_folder=str(tmp_path))
        monkeypatch.setattr("kai.config.settings.get_settings", lambda: settings)
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        instance_id = svc.deployment_instance_id(dep)

        (tmp_path / f"{instance_id}.json").write_text(
            json.dumps(
                {
                    "history": {
                        "chat-1": [{"role": "user", "content": "one"}],
                        "chat-2": [{"role": "user", "content": "two"}],
                    }
                }
            )
        )
        history_dir = tmp_path / f"{instance_id}.history"
        history_dir.mkdir()
        (history_dir / "chat-3.json").write_text(
            json.dumps(
                {
                    "key": "chat-3",
                    "messages": [
                        {"role": "user", "content": "three"},
                        {"role": "assistant", "content": "four"},
                    ],
                }
            )
        )

        assert svc.interaction_summary(dep) == (3, 4)
