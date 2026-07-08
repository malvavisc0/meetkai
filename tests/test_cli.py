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

        # configure() reads a bot config file that isn't present in the test
        # env; stub it so the test exercises the intended path (run() raising
        # BotStartupError -> "startup failed" panel).
        monkeypatch.setattr(Bot, "configure", lambda self, agent, settings: None)
        monkeypatch.setattr(Bot, "run", boom)
        result = runner.invoke(app, ["start", "waha"])
        assert result.exit_code != 0
        assert "startup failed" in result.stdout


class TestStopCommand:
    def test_stop_unknown_run_fails(self):
        result = runner.invoke(app, ["stop", "waha", "--run", "does-not-exist"])
        assert result.exit_code != 0
        assert "unknown or stale run" in result.stdout

    def test_stop_sends_sigterm(self, monkeypatch):
        from kai.cli import bot as cli_mod
        from kai.runs import RunRecord

        record = RunRecord(
            endpoint="http://x", hmac_key="k", hmac_algorithm="sha512", pid=12345, started_at="t"
        )
        monkeypatch.setattr(cli_mod, "_resolve_run", lambda bot, run, user="": record)

        sent = []
        monkeypatch.setattr(cli_mod.os, "kill", lambda pid, sig: sent.append((pid, sig)))

        result = runner.invoke(app, ["stop", "waha", "--run", "deadbeef"])
        assert result.exit_code == 0
        assert sent == [(12345, __import__("signal").SIGTERM)]
        assert "stopping" in result.stdout

    def test_stop_force_sends_sigkill(self, monkeypatch):
        from kai.cli import bot as cli_mod
        from kai.runs import RunRecord

        record = RunRecord(
            endpoint="http://x", hmac_key="k", hmac_algorithm="sha512", pid=12345, started_at="t"
        )
        monkeypatch.setattr(cli_mod, "_resolve_run", lambda bot, run, user="": record)

        sent = []
        monkeypatch.setattr(cli_mod.os, "kill", lambda pid, sig: sent.append((pid, sig)))

        result = runner.invoke(app, ["stop", "waha", "--run", "deadbeef", "--force"])
        assert result.exit_code == 0
        assert sent == [(12345, __import__("signal").SIGKILL)]
        assert "killed" in result.stdout

    def test_stop_already_gone_prunes(self, monkeypatch):
        from kai.cli import bot as cli_mod
        from kai.runs import RunRecord

        record = RunRecord(
            endpoint="http://x", hmac_key="k", hmac_algorithm="sha512", pid=12345, started_at="t"
        )
        monkeypatch.setattr(cli_mod, "_resolve_run", lambda bot, run, user="": record)
        monkeypatch.setattr(cli_mod.os, "kill", self._raise_not_found)
        removed = []
        monkeypatch.setattr(
            cli_mod.RunRegistry, "remove", lambda self, run_id: removed.append(run_id)
        )

        result = runner.invoke(app, ["stop", "waha", "--run", "deadbeef"])
        assert result.exit_code == 0
        assert removed == ["deadbeef"]
        assert "already gone" in result.stdout

    @staticmethod
    def _raise_not_found(pid, sig):
        raise ProcessLookupError()


class TestStatusCommand:
    def test_status_requires_run_id(self):
        result = runner.invoke(app, ["status", "waha"])
        # Missing required --run option
        assert result.exit_code != 0

    def test_status_unknown_run_fails(self):
        result = runner.invoke(app, ["status", "waha", "--run", "does-not-exist"])
        assert result.exit_code != 0
        assert "unknown or stale run" in result.stdout


class TestChatCommand:
    def test_chat_unknown_run_fails(self):
        result = runner.invoke(app, ["chat", "waha", "--run", "does-not-exist"])
        assert result.exit_code != 0
        assert "unknown or stale run" in result.stdout

    def test_chat_quit_exits_cleanly(self, monkeypatch):
        from kai.cli import bot as cli_mod
        from kai.runs import RunRecord

        record = RunRecord(
            endpoint="http://x", hmac_key="k", hmac_algorithm="sha512", pid=1, started_at="t"
        )
        monkeypatch.setattr(cli_mod, "_resolve_run", lambda bot, run, user="": record)

        inputs = iter(["/quit"])
        monkeypatch.setattr(cli_mod.console, "input", lambda *a, **k: next(inputs))
        calls = []

        def fake_post(record, message, *, persist):
            calls.append((message, persist))
            return 200, {"ok": True, "reply": "ack"}

        monkeypatch.setattr(cli_mod, "_post_tell", fake_post)

        result = runner.invoke(app, ["chat", "waha", "--run", "deadbeef"])
        assert result.exit_code == 0
        assert calls == []

    def test_chat_eof_exits(self, monkeypatch):
        from kai.cli import bot as cli_mod
        from kai.runs import RunRecord

        record = RunRecord(
            endpoint="http://x", hmac_key="k", hmac_algorithm="sha512", pid=1, started_at="t"
        )
        monkeypatch.setattr(cli_mod, "_resolve_run", lambda bot, run, user="": record)

        def raise_eof(*a, **k):
            raise EOFError

        monkeypatch.setattr(cli_mod.console, "input", raise_eof)
        result = runner.invoke(app, ["chat", "waha", "--run", "deadbeef"])
        assert result.exit_code == 0
        assert "bye" in result.stdout

    def test_chat_persist_toggle(self, monkeypatch):
        from kai.cli import bot as cli_mod
        from kai.runs import RunRecord

        record = RunRecord(
            endpoint="http://x", hmac_key="k", hmac_algorithm="sha512", pid=1, started_at="t"
        )
        monkeypatch.setattr(cli_mod, "_resolve_run", lambda bot, run, user="": record)

        inputs = iter(["/persist", "/quit"])
        monkeypatch.setattr(cli_mod.console, "input", lambda *a, **k: next(inputs))
        calls = []

        def fake_post(record, message, *, persist):
            calls.append((message, persist))
            return 200, {"ok": True, "reply": "ack"}

        monkeypatch.setattr(cli_mod, "_post_tell", fake_post)

        result = runner.invoke(app, ["chat", "waha", "--run", "deadbeef"])
        assert result.exit_code == 0
        assert calls == []
        assert "on" in result.stdout

    def test_chat_sends_message_and_renders(self, monkeypatch):
        from kai.cli import bot as cli_mod
        from kai.runs import RunRecord

        record = RunRecord(
            endpoint="http://x", hmac_key="k", hmac_algorithm="sha512", pid=1, started_at="t"
        )
        monkeypatch.setattr(cli_mod, "_resolve_run", lambda bot, run, user="": record)

        inputs = iter(["send a joke", "/quit"])
        monkeypatch.setattr(cli_mod.console, "input", lambda *a, **k: next(inputs))
        calls = []

        def fake_post(record, message, *, persist):
            calls.append((message, persist))
            return 200, {
                "ok": True,
                "reply": "here is a joke",
                "actions": [{"tool": "send_message", "chat_id": "g@g.us", "ok": True}],
            }

        monkeypatch.setattr(cli_mod, "_post_tell", fake_post)

        result = runner.invoke(app, ["chat", "waha", "--run", "deadbeef"])
        assert result.exit_code == 0
        assert calls == [("send a joke", False)]
        assert "here is a joke" in result.stdout
