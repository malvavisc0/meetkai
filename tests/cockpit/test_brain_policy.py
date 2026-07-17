"""Tests for per-deployment Brain policy (brain_mandatory / brain_instruction)."""

import subprocess

import pytest
from tests.cockpit.helpers import _connect_whatsapp

from kai.cockpit.deployments import DeploymentsService
from kai.cockpit.models import Connection


@pytest.fixture(autouse=True)
def _whatsapp_connected(user, db):
    """DeploymentsService.create() enforces required_connections — every
    test in this module needs a connected WhatsApp before it can create its
    ``waha`` deployment."""
    _connect_whatsapp(db, user)


def _brain_conn(user_id: int, *, instruction: str = "default rules") -> Connection:
    return Connection(
        user_id=user_id,
        service="lightrag",
        status="ready",
        config={
            "workspace": "kai-v001-bob_at_test_com",
            "instruction": instruction,
        },
        created_at="now",
        updated_at="now",
    )


class TestEditBrainPolicy:
    def test_edit_brain_mandatory_persists(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        svc.edit(dep, brain_mandatory=True)
        assert dep.brain_mandatory is True

    def test_edit_brain_mandatory_none_inherits(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        svc.edit(dep, brain_mandatory=True)
        svc.edit(dep, brain_mandatory=None)
        assert dep.brain_mandatory is None

    def test_edit_brain_instruction_persists(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        svc.edit(dep, brain_instruction="custom rules")
        assert dep.brain_instruction == "custom rules"

    def test_edit_brain_instruction_none_clears(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        svc.edit(dep, brain_instruction="custom rules")
        svc.edit(dep, brain_instruction=None)
        assert dep.brain_instruction is None

    def test_edit_brain_mandatory_rejects_non_bool(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        with pytest.raises(ValueError, match="brain_mandatory must be a bool"):
            svc.edit(dep, brain_mandatory="yes")

    def test_edit_brain_instruction_rejects_non_string(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        with pytest.raises(ValueError, match="brain_instruction must be a string"):
            svc.edit(dep, brain_instruction=42)


class _StartBrainPolicyBase:
    """Shared helpers for start() brain env tests."""

    @pytest.fixture(autouse=True)
    def _media_ready(self):
        from kai.cockpit.media_services import MEDIA_READY

        MEDIA_READY.set()
        yield
        MEDIA_READY.clear()

    def _start_and_capture_env(self, svc, dep, monkeypatch, tmp_path, user):
        monkeypatch.setattr("kai.cockpit.config_writer.write_config", lambda d, i: None)

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
        return captured_env


class TestStartBrainMandatory(_StartBrainPolicyBase):
    def test_mandatory_true_when_deployment_says_so(self, db, user, monkeypatch, tmp_path):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        svc.edit(dep, brain_mandatory=True)

        db.add(_brain_conn(user.id))
        db.commit()

        env = self._start_and_capture_env(svc, dep, monkeypatch, tmp_path, user)
        assert env["KAI_BRAIN_MANDATORY"] == "true"

    def test_mandatory_false_when_deployment_none(self, db, user, monkeypatch, tmp_path):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        # brain_mandatory is None by default

        db.add(_brain_conn(user.id))
        db.commit()

        env = self._start_and_capture_env(svc, dep, monkeypatch, tmp_path, user)
        assert env["KAI_BRAIN_MANDATORY"] == "false"

    def test_mandatory_false_when_deployment_false(self, db, user, monkeypatch, tmp_path):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        svc.edit(dep, brain_mandatory=False)

        db.add(_brain_conn(user.id))
        db.commit()

        env = self._start_and_capture_env(svc, dep, monkeypatch, tmp_path, user)
        assert env["KAI_BRAIN_MANDATORY"] == "false"


class TestStartBrainInstruction(_StartBrainPolicyBase):
    def test_deployment_override_wins(self, db, user, monkeypatch, tmp_path):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        svc.edit(dep, brain_instruction="custom rules")

        db.add(_brain_conn(user.id, instruction="default rules"))
        db.commit()

        env = self._start_and_capture_env(svc, dep, monkeypatch, tmp_path, user)
        assert env["KAI_BRAIN_INSTRUCTION"] == "custom rules"

    def test_falls_back_to_brain_default(self, db, user, monkeypatch, tmp_path):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        # brain_instruction is None → inherit from brain connection

        db.add(_brain_conn(user.id, instruction="default rules"))
        db.commit()

        env = self._start_and_capture_env(svc, dep, monkeypatch, tmp_path, user)
        assert env["KAI_BRAIN_INSTRUCTION"] == "default rules"

    def test_empty_override_falls_back(self, db, user, monkeypatch, tmp_path):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        svc.edit(dep, brain_instruction="   ")  # whitespace-only → falls back

        db.add(_brain_conn(user.id, instruction="default rules"))
        db.commit()

        env = self._start_and_capture_env(svc, dep, monkeypatch, tmp_path, user)
        assert env["KAI_BRAIN_INSTRUCTION"] == "default rules"


class TestStartBrainWorkspace(_StartBrainPolicyBase):
    def test_workspace_is_user_slug(self, db, user, monkeypatch, tmp_path):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")

        db.add(_brain_conn(user.id))
        db.commit()

        env = self._start_and_capture_env(svc, dep, monkeypatch, tmp_path, user)
        assert env["KAI_BRAIN_WORKSPACE"] == "kai-v001-bob_at_test_com"


class TestStartNoBrain(_StartBrainPolicyBase):
    def test_no_brain_env_vars_when_no_connection(self, db, user, monkeypatch, tmp_path):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")

        env = self._start_and_capture_env(svc, dep, monkeypatch, tmp_path, user)
        assert "KAI_BRAIN_WORKSPACE" not in env
        assert "KAI_BRAIN_INSTRUCTION" not in env
        assert "KAI_BRAIN_MANDATORY" not in env
