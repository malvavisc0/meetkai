"""CLI tests for `kai start --user/--voice` (docs/cockpit/01, 04)."""

import pytest
from typer.testing import CliRunner

from kai.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_run_registry(tmp_path, monkeypatch):
    """Keep `kai start`'s run-registry files out of the real ``data/`` dir."""
    from kai.cli import bot as cli_mod
    from kai.config.settings import Settings

    fake_settings = Settings.for_test(agent_history_folder=str(tmp_path))
    monkeypatch.setattr(cli_mod, "get_settings", lambda: fake_settings)


def _patch_minimal_bot_lifecycle(monkeypatch):
    """Make ``Bot.configure``/``run`` no-ops and ``tell_endpoint`` opt in.

    Mirrors the existing ``TestStartCommand`` pattern in tests/test_cli.py:
    patch the class methods so ``kai start`` completes a full cycle without
    touching a real WAHA instance.
    """
    from kai.bots.waha import Bot

    monkeypatch.setattr(Bot, "configure", lambda self, agent, settings, **kw: None)
    monkeypatch.setattr(Bot, "tell_endpoint", lambda self: "http://127.0.0.1:9999")
    monkeypatch.setattr(Bot, "tell_hmac_key", lambda self: "test-key")

    async def _run(self):
        return None

    monkeypatch.setattr(Bot, "run", _run)


class TestStartUserFlag:
    def test_user_flag_sets_instance_namespace(self, monkeypatch):
        from kai.bots import waha as waha_mod

        _patch_minimal_bot_lifecycle(monkeypatch)

        seen_instances = []
        original_init = waha_mod.Bot.__init__

        def _capture_instance(self, *a, **k):
            original_init(self, *a, **k)

        monkeypatch.setattr(waha_mod.Bot, "__init__", _capture_instance)

        # Capture the instance id actually used for the run registry by
        # spying on _runs_registry in cli.py.
        from kai.cli import bot as cli_mod

        original_registry = cli_mod._runs_registry

        def _spy_registry(bot_name, settings):
            seen_instances.append(bot_name)
            return original_registry(bot_name, settings)

        monkeypatch.setattr(cli_mod, "_runs_registry", _spy_registry)

        result = runner.invoke(app, ["start", "waha", "--user", "bob@example.com"])

        assert result.exit_code == 0
        assert "waha-bob@example.com" in seen_instances

    def test_no_user_flag_preserves_existing_behavior(self, monkeypatch):
        _patch_minimal_bot_lifecycle(monkeypatch)

        from kai.cli import bot as cli_mod

        seen_instances = []
        original_registry = cli_mod._runs_registry

        def _spy_registry(bot_name, settings):
            seen_instances.append(bot_name)
            return original_registry(bot_name, settings)

        monkeypatch.setattr(cli_mod, "_runs_registry", _spy_registry)

        result = runner.invoke(app, ["start", "waha"])

        assert result.exit_code == 0
        assert seen_instances
        assert set(seen_instances) == {"waha"}

    def test_kai_run_id_printed_to_stdout(self, monkeypatch):
        _patch_minimal_bot_lifecycle(monkeypatch)

        result = runner.invoke(app, ["start", "waha", "--user", "bob@example.com"])

        assert result.exit_code == 0
        assert "KAI_RUN_ID=" in result.stdout

    def test_voice_flag_passed_to_configure(self, monkeypatch):
        from kai.bots.waha import Bot

        seen_voice = {}

        def _capture_configure(self, agent, settings, *, voice=None, template=None, tools=None):
            seen_voice["value"] = voice

        monkeypatch.setattr(Bot, "configure", _capture_configure)
        monkeypatch.setattr(Bot, "tell_endpoint", lambda self: None)

        async def _run(self):
            return None

        monkeypatch.setattr(Bot, "run", _run)

        result = runner.invoke(app, ["start", "waha", "--voice", "custom_voice"])

        assert result.exit_code == 0
        assert seen_voice["value"] == "custom_voice"
