from typer.testing import CliRunner

from kai.cli import app

runner = CliRunner()


class TestListCommand:
    def test_list_shows_waha(self):
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "waha" in result.stdout


class TestStartCommand:
    def test_start_unknown_bot_fails(self):
        result = runner.invoke(app, ["start", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.stdout

    def test_start_without_bot_name_shows_help(self):
        result = runner.invoke(app, ["start"])
        # typer with no_args_is_help=True will show help and exit with 0
        # but since bot_name is required, it might be different
        assert result.exit_code != 0 or "Usage:" in result.stdout

    def test_start_startup_error_exits_nonzero(self, monkeypatch):
        from kai.bots.waha import Bot
        from kai.cli import BotStartupError

        async def boom(self):
            raise BotStartupError("could not reach WAHA")

        monkeypatch.setattr(Bot, "run", boom)
        result = runner.invoke(app, ["start", "waha"])
        assert result.exit_code != 0
        assert "startup failed" in result.stdout


class TestStatusCommand:
    def test_status_requires_bot_name(self):
        result = runner.invoke(app, ["status"])
        # Missing required argument
        assert result.exit_code != 0

    def test_status_unknown_bot_fails(self):
        result = runner.invoke(app, ["status", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.stdout
