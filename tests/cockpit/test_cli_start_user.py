"""CLI tests for `kai start --user` (docs/cockpit/01, 04)."""

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


@pytest.fixture(autouse=True)
def _brain_env(monkeypatch):
    """The email ``general`` template declares ``brain_query`` as a required
    tool, so the boot gate checks for Brain env vars. Provide stubs so the
    gate passes without a real Morphik server (registration is best-effort
    and wrapped in a try/except in ``_start``)."""
    monkeypatch.setenv("KAI_BRAIN_BASE_URL", "http://brain.example.com")
    monkeypatch.setenv("KAI_BRAIN_MORPHIK_TOKEN", "stub-token")


def _patch_minimal_bot_lifecycle(monkeypatch):
    """Make ``Bot.configure``/``run`` no-ops and ``tell_endpoint`` opt in.

    Patches the email ``Bot`` class methods so ``kai start`` completes a full
    cycle without touching a real provider.
    """
    from kai.bots.email import Bot

    monkeypatch.setattr(Bot, "configure", lambda self, agent, settings, **kw: None)
    monkeypatch.setattr(Bot, "tell_endpoint", lambda self: "http://127.0.0.1:9999")
    monkeypatch.setattr(Bot, "tell_hmac_key", lambda self: "test-key")

    async def _run(self):
        return None

    monkeypatch.setattr(Bot, "run", _run)


class TestStartUserFlag:
    def test_user_flag_sets_instance_namespace(self, monkeypatch):
        from kai.bots import email as email_mod

        _patch_minimal_bot_lifecycle(monkeypatch)

        seen_instances = []
        original_init = email_mod.Bot.__init__

        def _capture_instance(self, *a, **k):
            original_init(self, *a, **k)

        monkeypatch.setattr(email_mod.Bot, "__init__", _capture_instance)

        # Capture the instance id actually used for the run registry by
        # spying on _runs_registry in cli.py.
        from kai.cli import bot as cli_mod

        original_registry = cli_mod._runs_registry

        def _spy_registry(bot_name, settings):
            seen_instances.append(bot_name)
            return original_registry(bot_name, settings)

        monkeypatch.setattr(cli_mod, "_runs_registry", _spy_registry)

        result = runner.invoke(app, ["start", "email", "--user", "bob@example.com"])

        assert result.exit_code == 0
        assert "email-bob@example.com" in seen_instances

    def test_no_user_flag_preserves_existing_behavior(self, monkeypatch):
        _patch_minimal_bot_lifecycle(monkeypatch)

        from kai.cli import bot as cli_mod

        seen_instances = []
        original_registry = cli_mod._runs_registry

        def _spy_registry(bot_name, settings):
            seen_instances.append(bot_name)
            return original_registry(bot_name, settings)

        monkeypatch.setattr(cli_mod, "_runs_registry", _spy_registry)

        result = runner.invoke(app, ["start", "email"])

        assert result.exit_code == 0
        assert seen_instances
        assert set(seen_instances) == {"email"}

    def test_kai_run_id_printed_to_stdout(self, monkeypatch):
        _patch_minimal_bot_lifecycle(monkeypatch)

        result = runner.invoke(app, ["start", "email", "--user", "bob@example.com"])

        assert result.exit_code == 0
        assert "KAI_RUN_ID=" in result.stdout
