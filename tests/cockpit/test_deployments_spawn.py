"""Tests for argv passed when spawning a bot subprocess with template/tool overrides."""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest
from tests.cockpit.helpers import _connect_whatsapp

from kai.cockpit.deployments import DeploymentsService


@pytest.fixture(autouse=True)
def _whatsapp_connected(user, db):
    _connect_whatsapp(db, user)


@pytest.fixture(autouse=True)
def _media_ready():
    from kai.cockpit.media_services import MEDIA_READY

    MEDIA_READY.set()
    yield
    MEDIA_READY.clear()


@pytest.fixture(autouse=True)
def _patch_config_writer(monkeypatch):
    monkeypatch.setattr("kai.cockpit.config_writer.write_config", lambda dep, instance_id: None)


def _setup_popen(monkeypatch, tmp_path, dep_db, dep_user) -> tuple[list[str], Callable]:
    """Set up a Popen mock that captures argv. Returns (captured list, dep+svc factory)."""
    captured: list[str] = []

    def fake_popen(argv, env=None, **kwargs):
        captured[:] = argv

        class FakeProc:
            returncode = None
            _lines = iter(["starting...\n", "KAI_RUN_ID=deadbeef\n"])

            @property
            def stdout(self):
                return self

            def readline(self):
                return next(self._lines, "")

            def poll(self):
                return self.returncode

        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    from kai.config.settings import Settings
    from kai.runs import RunRecord, RunRegistry, runs_path

    fake_settings = Settings.for_test(agent_history_folder=str(tmp_path))
    monkeypatch.setattr("kai.config.settings.get_settings", lambda: fake_settings)

    instance_id = f"waha-{dep_user.email}"
    registry = RunRegistry(runs_path(fake_settings.agent_history_folder, instance_id))
    registry.replace(
        "deadbeef",
        RunRecord(
            endpoint="http://x",
            hmac_key="k",
            hmac_algorithm="sha512",
            pid=1,
            started_at="t",
        ),
    )

    def deploy_and_start(template: str = "general", tool_overrides: dict | None = None):
        svc = DeploymentsService(dep_db)
        dep = svc.create(dep_user, "waha", "be helpful", "English", template=template)
        if tool_overrides is not None:
            svc.edit(dep, tool_overrides=tool_overrides)
        svc.start(dep)
        return dep, svc

    return captured, deploy_and_start


class TestSpawnTemplate:
    def test_default_template_in_argv(self, db, user, monkeypatch, tmp_path):
        captured, deploy_and_start = _setup_popen(monkeypatch, tmp_path, db, user)
        deploy_and_start(template="general")
        assert "--template" in captured
        idx = captured.index("--template")
        assert captured[idx + 1] == "general"

    def test_custom_template_in_argv(self, db, user, monkeypatch, tmp_path):
        captured, deploy_and_start = _setup_popen(monkeypatch, tmp_path, db, user)
        deploy_and_start(template="customer-support")
        assert "--template" in captured
        idx = captured.index("--template")
        assert captured[idx + 1] == "customer-support"

    def test_enable_tools_in_argv(self, db, user, monkeypatch, tmp_path):
        captured, deploy_and_start = _setup_popen(monkeypatch, tmp_path, db, user)
        deploy_and_start(
            template="general",
            tool_overrides={"enable": ["web_search"], "disable": []},
        )
        idx = captured.index("--enable-tools")
        assert captured[idx + 1] == "web_search"

    def test_disable_tools_in_argv(self, db, user, monkeypatch, tmp_path):
        captured, deploy_and_start = _setup_popen(monkeypatch, tmp_path, db, user)
        deploy_and_start(
            template="general",
            tool_overrides={"enable": [], "disable": ["web_search"]},
        )
        idx = captured.index("--disable-tools")
        assert captured[idx + 1] == "web_search"

    def test_multiple_tools_in_argv(self, db, user, monkeypatch, tmp_path):
        captured, deploy_and_start = _setup_popen(monkeypatch, tmp_path, db, user)
        deploy_and_start(
            template="general",
            tool_overrides={"enable": ["web_search", "calculate"], "disable": []},
        )
        assert "--enable-tools" in captured
        enable_idx = captured.index("--enable-tools")
        assert captured[enable_idx + 1] == "web_search"
        assert captured[enable_idx + 3] == "calculate"

    def test_empty_tool_overrides_no_tool_flags(self, db, user, monkeypatch, tmp_path):
        captured, deploy_and_start = _setup_popen(monkeypatch, tmp_path, db, user)
        deploy_and_start(template="general", tool_overrides={})
        assert "--enable-tools" not in captured
        assert "--disable-tools" not in captured


class TestEditToolOverrides:
    def test_edit_template(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        svc.edit(dep, template="customer-support")
        assert dep.template == "customer-support"

    def test_edit_tool_overrides(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        svc.edit(dep, tool_overrides={"enable": ["web_search"], "disable": []})
        assert dep.tool_overrides == {"enable": ["web_search"], "disable": []}

    def test_edit_tool_overrides_rejects_unknown_keys(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        with pytest.raises(ValueError, match="only have 'enable' and 'disable' keys"):
            svc.edit(dep, tool_overrides={"foo": "bar"})

    def test_edit_tool_overrides_rejects_non_dict(self, db, user):
        svc = DeploymentsService(db)
        dep = svc.create(user, "waha", "goal", "English")
        with pytest.raises(ValueError, match="must be a dict"):
            svc.edit(dep, tool_overrides="not-a-dict")
