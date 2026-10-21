"""Tests for kai.cockpit.deployments.DeploymentsService."""

import subprocess

import pytest
from tests.cockpit.helpers import make_fake_proc, seed_run_registry

from kai.cockpit.deployments import (
    ConnectionRequiredError,
    DeploymentsService,
    DeploymentStartupError,
)
from kai.cockpit.models import Connection

pytestmark = pytest.mark.usefixtures("email_ready_user")


def _start_with_morphik_connection(db, user, monkeypatch, tmp_path) -> dict:
    svc = DeploymentsService(db)
    dep = svc.create(user, "email", "goal", "English")
    svc.edit(dep, brain_mandatory=True)

    db.add(
        Connection(
            user_id=user.id,
            service="morphik",
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

    def fake_popen(*args, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return make_fake_proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    instance_id = f"{dep.bot_type}-{user.email}"
    fake_settings, _ = seed_run_registry(tmp_path, instance_id)
    monkeypatch.setattr("kai.config.settings.get_settings", lambda: fake_settings)

    svc.start(dep)

    return captured_env


class TestStart:
    def test_start_rejects_disconnected_connection(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        # Disconnect after creation to prove start() re-checks the
        # connection independently rather than trusting create()'s
        # one-time check (an operator can disconnect any time
        # after creating the bot).
        conn = db.query(Connection).filter_by(user_id=user.id, service="resend").first()
        conn.status = "disconnected"
        db.commit()
        with pytest.raises(ConnectionRequiredError):
            svc.start(dep)

    def test_start_spawns_subprocess_and_registers_run(self, db, user, monkeypatch, tmp_path):
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")

        monkeypatch.setattr("kai.cockpit.config_writer.write_config", lambda dep, instance_id: None)

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: make_fake_proc())

        instance_id = f"{dep.bot_type}-{user.email}"
        fake_settings, _ = seed_run_registry(tmp_path, instance_id)
        monkeypatch.setattr("kai.config.settings.get_settings", lambda: fake_settings)

        svc.start(dep)

        assert dep.run_id == "deadbeef"
        assert dep.status == "running"
        assert dep.desired_state == "running"

    def test_start_injects_brain_env_when_morphik_connection_exists(
        self, db, user, monkeypatch, tmp_path
    ):
        captured_env = _start_with_morphik_connection(db, user, monkeypatch, tmp_path)

        assert captured_env["KAI_BRAIN_WORKSPACE"] == "kai-v001-bob_at_test_com"
        assert captured_env["KAI_BRAIN_INSTRUCTION"] == "how to do X from section Y"
        assert captured_env["KAI_BRAIN_MANDATORY"] == "true"

    def test_start_omits_brain_env_when_no_morphik_connection(
        self, db, user, monkeypatch, tmp_path
    ):
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")

        monkeypatch.setattr("kai.cockpit.config_writer.write_config", lambda dep, instance_id: None)

        captured_env: dict = {}

        def fake_popen(*args, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            return make_fake_proc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        instance_id = f"{dep.bot_type}-{user.email}"
        fake_settings, _ = seed_run_registry(tmp_path, instance_id)
        monkeypatch.setattr("kai.config.settings.get_settings", lambda: fake_settings)

        svc.start(dep)

        assert "KAI_BRAIN_WORKSPACE" not in captured_env

    def test_start_raises_on_process_exit_without_run_id(self, db, user, monkeypatch):
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")

        monkeypatch.setattr("kai.cockpit.config_writer.write_config", lambda dep, instance_id: None)

        monkeypatch.setattr(
            subprocess,
            "Popen",
            lambda *a, **k: make_fake_proc(lines=[], returncode=1),
        )

        with pytest.raises(DeploymentStartupError):
            svc.start(dep)


class TestStop:
    def test_stop_with_no_run_id_just_marks_stopped(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        svc.stop(dep)
        assert dep.status == "stopped"
        assert dep.desired_state == "stopped"

    def test_stop_after_start_resets_desired_state(self, db, user):
        """A deployment stopped by the user must not be relaunched by
        ``reconcile_deployments`` on the next cockpit startup — regression
        test for a bug where ``stop()`` left ``desired_state == "running"``
        forever after the first ``start()``, so every deployment ever
        started (even ones later stopped) was resurrected on restart."""
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        dep.status = "running"
        dep.desired_state = "running"
        db.commit()

        svc.stop(dep)

        assert dep.status == "stopped"
        assert dep.desired_state == "stopped"

    def test_stop_with_dead_record_does_not_kill(self, db, user, monkeypatch, tmp_path):
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        dep.run_id = "deadbeef"
        db.commit()

        instance_id = f"{dep.bot_type}-{user.email}"
        fake_settings, _ = seed_run_registry(tmp_path, instance_id, pid_start=1)
        monkeypatch.setattr("kai.config.settings.get_settings", lambda: fake_settings)

        killed = []
        monkeypatch.setattr(
            "kai.cockpit.deployments.os.kill", lambda pid, sig: killed.append((pid, sig))
        )

        svc.stop(dep)

        assert dep.status == "stopped"
        assert dep.run_id is None
        assert killed == []

    def test_stop_leaves_foreign_live_record_intact(self, db, user, monkeypatch, tmp_path):
        """A live registry record belonging to a different run_id (e.g. a
        manually started bot on this instance) must not be killed or pruned
        when this deployment is stopped."""
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        dep.run_id = "ours"
        db.commit()

        instance_id = f"{dep.bot_type}-{user.email}"
        fake_settings, registry = seed_run_registry(tmp_path, instance_id, run_id="theirs")
        monkeypatch.setattr("kai.config.settings.get_settings", lambda: fake_settings)

        killed = []
        monkeypatch.setattr(
            "kai.cockpit.deployments.os.kill", lambda pid, sig: killed.append((pid, sig))
        )

        svc.stop(dep)

        assert dep.status == "stopped"
        assert dep.run_id is None
        assert killed == []
        # The foreign live record is untouched: still resolvable and live.
        record = registry.get()
        assert record is not None and record.run_id == "theirs"


class TestDelete:
    def test_delete_removes_row(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        dep_id = dep.id

        svc.delete(dep)

        assert svc.get(dep_id) is None

    def test_delete_stops_running_bot_first(self, db, user, monkeypatch, tmp_path):
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        dep.run_id = "deadbeef"
        dep.status = "running"
        db.commit()

        instance_id = f"{dep.bot_type}-{user.email}"
        fake_settings, registry = seed_run_registry(tmp_path, instance_id, pid_start=1)
        monkeypatch.setattr("kai.config.settings.get_settings", lambda: fake_settings)

        svc.delete(dep)

        assert svc.get(dep.id) is None
        assert registry.get() is None

    def test_delete_removes_config_file(self, db, user, monkeypatch, tmp_path):
        from kai.cockpit import config_writer

        configs_dir = tmp_path / "configs" / "cockpit"
        monkeypatch.setattr(config_writer, "CONFIGS_DIR", configs_dir)

        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        instance_id = f"{dep.bot_type}-{user.email}"
        config_writer.write_config(dep, instance_id)
        config_path = configs_dir / f"{instance_id}.json"
        assert config_path.exists()

        svc.delete(dep)

        assert not config_path.exists()
        assert svc.get(dep.id) is None

    def test_delete_keeps_email_connection(self, db, user):
        """Deleting a deployment must NOT touch the resend Connection."""
        svc = DeploymentsService(db)
        dep = svc.create(user, "email", "goal", "English")
        svc.delete(dep)

        assert db.query(Connection).filter_by(user_id=user.id, service="resend").first() is not None

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
        dep = svc.create(user, "email", "goal", "English")
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
        history_dir = data_dir / f"{instance_id}.history"
        history_dir.mkdir()
        (history_dir / "conversation.json").write_text('{"key": "chat", "messages": []}')
        config_path = configs_dir / f"{instance_id}.json"
        assert config_path.exists()
        files.append(config_path)

        svc.delete(dep)

        for path in files:
            assert not path.exists(), f"State file not purged: {path}"
        assert not history_dir.exists(), "History directory not purged"
        assert svc.get(dep.id) is None
