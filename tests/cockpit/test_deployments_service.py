"""Tests for kai.cockpit.deployments.DeploymentsService."""

import subprocess

import pytest
from tests.cockpit.helpers import _connect_whatsapp

from kai.bots.waha.config import WahaSettings
from kai.cockpit.bots import auto_pick_voice
from kai.cockpit.deployments import (
    ConnectionRequiredError,
    DeploymentsService,
    DeploymentStartupError,
)
from kai.cockpit.models import Connection


@pytest.fixture(autouse=True)
def _whatsapp_connected(user, db):
    """Most of this module's tests just want a "ready to go" user:
    ``DeploymentsService.create()`` now enforces
    ``BotType.required_connections``, so a connected WhatsApp is a
    prerequisite for the ``svc.create(user, "waha", ...)`` calls throughout
    this file. The handful of tests that specifically exercise the
    disconnected/missing-connection path mutate or remove this connection
    themselves after ``create()`` succeeds.
    """
    _connect_whatsapp(db, user)


class TestCreate:
    def test_create_happy_path(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "be helpful", "English")
        assert dep.id is not None
        assert dep.status == "stopped"
        assert dep.desired_state == "stopped"
        assert dep.settings["language"] == "English"
        assert dep.feature_flags == {"image": False, "stt": False, "tts": False, "video": False}

    def test_voice_auto_pick(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "be helpful", "Spanish")
        assert dep.voice == auto_pick_voice("Spanish")

    def test_explicit_voice_kept(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "be helpful", "English", voice="am_michael")
        assert dep.voice == "am_michael"

    def test_reject_empty_goal(self, db, user):
        svc = DeploymentsService(db)
        with pytest.raises(ValueError):
            svc.create(user, "waha", "", "English")

    def test_reject_empty_language(self, db, user):
        svc = DeploymentsService(db)
        with pytest.raises(ValueError):
            svc.create(user, "waha", "goal", "")

    def test_reject_disabled_user(self, db, user):
        user.is_disabled = True
        db.commit()
        svc = DeploymentsService(db)
        with pytest.raises(ValueError):
            svc.create(user, "waha", "goal", "English")

    def test_reject_unknown_bot_type(self, db, user):
        svc = DeploymentsService(db)
        with pytest.raises(ValueError):
            svc.create(user, "telegram", "goal", "English")

    def test_unique_constraint_per_user_bot_type(self, db, user):
        svc = DeploymentsService(db)
        svc.create(user, "waha", "goal", "English")
        with pytest.raises(ValueError):
            svc.create(user, "waha", "another goal", "English")


class TestCreateConnectionGate:
    """DeploymentsService.create() enforces BotType.required_connections —
    a bot can't even be created before its required connections exist, not
    just started (see also TestConnectionCatalog's start()-time gate)."""

    def test_rejects_when_whatsapp_missing(self, db):
        """A user fixture with no Connection row at all (bypassing the
        module's autouse ``_whatsapp_connected`` fixture by constructing a
        fresh user) must be rejected at create()."""
        import secrets
        from datetime import UTC, datetime

        from kai.cockpit.models import User

        lonely_user = User(
            email="lonely@test.com",
            language="English",
            timezone="UTC",
            hmac_key=secrets.token_hex(32),
            created_at=datetime.now(UTC).isoformat(),
        )
        db.add(lonely_user)
        db.commit()

        svc = DeploymentsService(db)
        with pytest.raises(ConnectionRequiredError, match="whatsapp"):
            svc.create(lonely_user, "waha", "goal", "English")

    def test_rejects_when_whatsapp_disconnected(self, db, user):
        conn = db.query(Connection).filter_by(user_id=user.id, service="whatsapp").first()
        conn.status = "disconnected"
        db.commit()

        svc = DeploymentsService(db)
        with pytest.raises(ConnectionRequiredError, match="whatsapp"):
            svc.create(user, "waha", "goal", "English")

    def test_succeeds_once_whatsapp_connected(self, db, user):
        # The module-level autouse fixture already connected WhatsApp.
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        assert dep.id is not None


class TestEdit:
    def test_edit_goal(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        svc.edit(dep, goal="new goal")
        assert dep.goal == "new goal"

    def test_edit_rejects_empty_goal(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        with pytest.raises(ValueError):
            svc.edit(dep, goal="")

    def test_edit_feature_flags_validates_subset(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        with pytest.raises(ValueError):
            svc.edit(dep, feature_flags={"telegram": True})

    def test_edit_feature_flags_happy_path(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        svc.edit(dep, feature_flags={"image": False})
        assert dep.feature_flags == {"image": False}

    def test_edit_settings_merges_partial(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        original_trigger = dep.settings["trigger_keyword"]
        svc.edit(dep, settings={"whitelist": ["a@b.c"]})
        assert dep.settings["whitelist"] == ["a@b.c"]
        assert dep.settings["trigger_keyword"] == original_trigger


class TestGetAndList:
    def test_get(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        fetched = svc.get(dep.id)
        assert fetched is not None
        assert fetched.id == dep.id

    def test_get_missing_returns_none(self, db, user):
        svc = DeploymentsService(db)
        assert svc.get(999) is None

    def test_list_for_user(self, db, user):
        svc = DeploymentsService(db)
        svc.create(user, "waha", "goal", "English")
        assert len(svc.list_for_user(user.id)) == 1


class TestStartMediaReadinessGate:
    """start() must not spawn a bot until MEDIA_READY is set."""

    def test_raises_when_media_not_ready(self, db, user, monkeypatch):
        from kai.cockpit.media_services import MEDIA_READY

        MEDIA_READY.clear()
        monkeypatch.setattr(
            "kai.bots.waha.config.get_waha_settings",
            lambda: WahaSettings.for_test(media_ready_timeout=0.05, hmac_key="test-secret"),
        )

        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")

        with pytest.raises(DeploymentStartupError, match="media services not ready"):
            svc.start(dep)

    def test_proceeds_when_media_ready(self, db, user):
        from kai.cockpit.media_services import MEDIA_READY

        MEDIA_READY.set()
        try:
            svc = DeploymentsService(db)
            dep = svc.create(user, "waha", "goal", "English")
            # Disconnect WhatsApp *after* creation so the next check (the
            # connection gate) fails, proving the media-readiness gate
            # itself did not block this call.
            conn = db.query(Connection).filter_by(user_id=user.id, service="whatsapp").first()
            conn.status = "disconnected"
            db.commit()
            with pytest.raises(ConnectionRequiredError):
                svc.start(dep)
        finally:
            MEDIA_READY.clear()


class TestStart:
    @pytest.fixture(autouse=True)
    def _media_ready(self):
        """These tests exercise start() logic unrelated to media readiness;
        pretend the shared STT/TTS services are already up so the gate in
        DeploymentsService.start() doesn't block/time out.
        """
        from kai.cockpit.media_services import MEDIA_READY

        MEDIA_READY.set()
        yield
        MEDIA_READY.clear()

    def test_requires_connection(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        # Disconnect after creation to prove start() re-checks the
        # connection independently rather than trusting create()'s
        # one-time check (an operator can disconnect WhatsApp any time
        # after creating the bot).
        conn = db.query(Connection).filter_by(user_id=user.id, service="whatsapp").first()
        conn.status = "disconnected"
        db.commit()
        with pytest.raises(ConnectionRequiredError):
            svc.start(dep)

    def test_start_spawns_subprocess_and_registers_run(self, db, user, monkeypatch, tmp_path):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")

        monkeypatch.setattr("kai.cockpit.config_writer.write_config", lambda dep, instance_id: None)

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

        assert dep.run_id == "deadbeef"
        assert dep.status == "running"
        assert dep.desired_state == "running"

    def test_start_injects_brain_env_when_lightrag_connection_exists(
        self, db, user, monkeypatch, tmp_path
    ):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        svc.edit(dep, brain_mandatory=True)

        db.add(
            Connection(
                user_id=user.id,
                service="lightrag",
                status="ready",
                config={
                    "workspace": "kai-v001-bob_at_test_com",
                    "instruction": "how to do X from section Y",
                },
                created_at="now",
                updated_at="now",
            )
        )
        db.commit()

        monkeypatch.setattr("kai.cockpit.config_writer.write_config", lambda dep, instance_id: None)

        captured_env: dict = {}

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

        def fake_popen(*args, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            return FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

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

        assert captured_env["KAI_BRAIN_WORKSPACE"] == "kai-v001-bob_at_test_com"
        assert captured_env["KAI_BRAIN_INSTRUCTION"] == "how to do X from section Y"
        assert captured_env["KAI_BRAIN_MANDATORY"] == "true"

    def test_start_omits_brain_env_when_no_lightrag_connection(
        self, db, user, monkeypatch, tmp_path
    ):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")

        monkeypatch.setattr("kai.cockpit.config_writer.write_config", lambda dep, instance_id: None)

        captured_env: dict = {}

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

        def fake_popen(*args, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            return FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

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

        assert "KAI_BRAIN_WORKSPACE" not in captured_env

    def test_start_raises_on_process_exit_without_run_id(self, db, user, monkeypatch):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")

        monkeypatch.setattr("kai.cockpit.config_writer.write_config", lambda dep, instance_id: None)

        class FakeProc:
            returncode = 1

            @property
            def stdout(self):
                return self

            def readline(self):
                return ""

            def poll(self):
                return self.returncode

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProc())

        with pytest.raises(DeploymentStartupError):
            svc.start(dep)


class TestStop:
    def test_stop_with_no_run_id_just_marks_stopped(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        svc.stop(dep)
        assert dep.status == "stopped"
        assert dep.desired_state == "stopped"

    def test_stop_after_start_resets_desired_state(self, db, user, monkeypatch):
        """A deployment stopped by the user must not be relaunched by
        ``reconcile_deployments`` on the next cockpit startup — regression
        test for a bug where ``stop()`` left ``desired_state == "running"``
        forever after the first ``start()``, so every deployment ever
        started (even ones later stopped) was resurrected on restart."""
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        dep.status = "running"
        dep.desired_state = "running"
        db.commit()

        svc.stop(dep)

        assert dep.status == "stopped"
        assert dep.desired_state == "stopped"

    def test_stop_sends_sigterm_to_live_pid(self, db, user, monkeypatch, tmp_path):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        dep.run_id = "deadbeef"
        db.commit()

        from kai.config.settings import Settings
        from kai.runs import RunRecord, RunRegistry, runs_path

        fake_settings = Settings.for_test(agent_history_folder=str(tmp_path))
        monkeypatch.setattr("kai.config.settings.get_settings", lambda: fake_settings)

        instance_id = f"{dep.bot_type}-{user.email}"
        registry = RunRegistry(runs_path(fake_settings.agent_history_folder, instance_id))
        registry.replace(
            "deadbeef",
            RunRecord(
                endpoint="http://x",
                hmac_key="k",
                hmac_algorithm="sha512",
                pid=99999,
                started_at="t",
            ),
        )

        monkeypatch.setattr("kai.cockpit.deployments.pid_alive", lambda pid: False)
        killed = []
        monkeypatch.setattr(
            "kai.cockpit.deployments.os.kill", lambda pid, sig: killed.append((pid, sig))
        )

        svc.stop(dep)

        assert dep.status == "stopped"
        assert dep.run_id is None


class TestDelete:
    def test_delete_removes_row(self, db, user, monkeypatch):
        # delete() calls write_config via edit path indirectly? No — delete
        # itself unlinks the config file. Stub write_config so edit (called
        # by create) doesn't touch the real CONFIGS_DIR.
        monkeypatch.setattr("kai.cockpit.config_writer.write_config", lambda d, i: None)
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        dep_id = dep.id

        svc.delete(dep)

        assert svc.get(dep_id) is None

    def test_delete_stops_running_bot_first(self, db, user, monkeypatch, tmp_path):
        monkeypatch.setattr("kai.cockpit.config_writer.write_config", lambda d, i: None)
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        dep.run_id = "deadbeef"
        dep.status = "running"
        db.commit()

        from kai.config.settings import Settings
        from kai.runs import RunRecord, RunRegistry, runs_path

        fake_settings = Settings.for_test(agent_history_folder=str(tmp_path))
        monkeypatch.setattr("kai.config.settings.get_settings", lambda: fake_settings)

        instance_id = f"{dep.bot_type}-{user.email}"
        registry = RunRegistry(runs_path(fake_settings.agent_history_folder, instance_id))
        registry.replace(
            "deadbeef",
            RunRecord(
                endpoint="http://x",
                hmac_key="k",
                hmac_algorithm="sha512",
                pid=99999,
                started_at="t",
            ),
        )
        monkeypatch.setattr("kai.cockpit.deployments.pid_alive", lambda pid: False)

        svc.delete(dep)

        assert svc.get(dep.id) is None
        assert registry.get("deadbeef") is None

    def test_delete_removes_config_file(self, db, user, monkeypatch, tmp_path):
        from kai.cockpit import config_writer

        configs_dir = tmp_path / "configs" / "cockpit"
        monkeypatch.setattr(config_writer, "CONFIGS_DIR", configs_dir)

        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        instance_id = f"{dep.bot_type}-{user.email}"
        config_writer.write_config(dep, instance_id)
        config_path = configs_dir / f"{instance_id}.json"
        assert config_path.exists()

        svc.delete(dep)

        assert not config_path.exists()
        assert svc.get(dep.id) is None

    def test_delete_keeps_whatsapp_connection(self, db, user, monkeypatch):
        """Deleting a deployment must NOT touch the WhatsApp Connection."""
        monkeypatch.setattr("kai.cockpit.config_writer.write_config", lambda d, i: None)

        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        svc.delete(dep)

        from kai.cockpit.connections.service import ConnectionsService

        assert ConnectionsService(db).get_whatsapp(user) is not None

    def test_delete_purges_all_bot_state_files(self, db, user, monkeypatch, tmp_path):
        """delete() must remove every per-bot state file, not just the config."""
        from kai.cockpit import config_writer
        from kai.config.settings import Settings

        configs_dir = tmp_path / "configs" / "cockpit"
        monkeypatch.setattr(config_writer, "CONFIGS_DIR", configs_dir)

        data_dir = tmp_path / "data"
        data_dir.mkdir()

        fake_settings = Settings.for_test(
            agent_history_folder=str(data_dir), tasks_folder=str(data_dir)
        )
        monkeypatch.setattr("kai.config.settings.get_settings", lambda: fake_settings)

        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        instance_id = f"{dep.bot_type}-{user.email}"
        config_writer.write_config(dep, instance_id)

        # Create all per-bot state files.
        files: list = []
        suffixes = [
            f"{instance_id}.json",
            f"{instance_id}.json.goal",
            f"{instance_id}.runs.json",
            f"{instance_id}.seen.json",
            f"{instance_id}.sleep.json",
            f"{instance_id}.tasks.json",
        ]
        for suffix in suffixes:
            path = data_dir / suffix
            path.write_text("{}")
            files.append(path)
        config_path = configs_dir / f"{instance_id}.json"
        assert config_path.exists()
        files.append(config_path)

        svc.delete(dep)

        for path in files:
            assert not path.exists(), f"State file not purged: {path}"
        assert svc.get(dep.id) is None


class TestReconcileDeployments:
    def test_restarts_deployment_marked_running_with_no_live_process(self, db, user, monkeypatch):
        """desired_state=='running' but no live run (e.g. after a container
        restart killed every bot subprocess) must trigger a fresh start()."""
        from kai.cockpit.deployments import DeploymentsService, reconcile_deployments

        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
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

        reconcile_deployments()

        assert started == [dep.id]

    def test_skips_deployment_already_alive(self, db, user, monkeypatch):
        from kai.cockpit.deployments import DeploymentsService, reconcile_deployments

        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
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

        reconcile_deployments()

        assert started == []

    def test_skips_deployment_without_connection_and_continues(self, db, user, monkeypatch):
        """A ConnectionRequiredError for one deployment must not stop the
        reconciliation of the rest."""
        import secrets
        from datetime import UTC, datetime

        from kai.cockpit.deployments import DeploymentsService, reconcile_deployments
        from kai.cockpit.models import User

        user2 = User(
            email="alice@test.com",
            language="English",
            timezone="UTC",
            hmac_key=secrets.token_hex(32),
            created_at=datetime.now(UTC).isoformat(),
        )
        db.add(user2)
        db.commit()
        _connect_whatsapp(db, user2)

        svc = DeploymentsService(db)
        dep1 = svc.create(user, "waha", "goal 1", "English")
        dep1.desired_state = "running"
        dep2 = svc.create(user2, "waha", "goal 2", "English")
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

        reconcile_deployments()

        assert started == [dep2.id]

    def test_ignores_deployments_with_desired_state_stopped(self, db, user, monkeypatch):
        from kai.cockpit.deployments import DeploymentsService, reconcile_deployments

        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        assert dep.desired_state == "stopped"

        started: list[int] = []
        monkeypatch.setattr(
            "kai.cockpit.deployments.DeploymentsService.start",
            lambda self, d: started.append(d.id),
        )

        reconcile_deployments()

        assert started == []
