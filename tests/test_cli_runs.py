import os
import types

import pytest
import typer

from kai import runs as runs_mod
from kai.cli import bot as cli_bot
from kai.runs import RunRecord, RunRegistry, proc_start_time, record_is_live, sanitize_instance_id

_SELF_START = proc_start_time(os.getpid())
needs_proc = pytest.mark.skipif(_SELF_START is None, reason="requires /proc")


def _record(pid_start: int | None) -> RunRecord:
    return RunRecord(
        run_id="deadbeef",
        endpoint="http://x",
        hmac_key="k",
        pid=os.getpid(),
        started_at="t",
        pid_start=pid_start,
    )


class TestSanitizeInstanceId:
    def test_keeps_normal_email(self):
        assert sanitize_instance_id("email-alice@ex.com") == "email-alice@ex.com"

    def test_strips_path_separators(self):
        cleaned = sanitize_instance_id("../../etc/passwd")
        assert "/" not in cleaned
        assert cleaned == ".._.._etc_passwd"

    def test_empty_becomes_unknown(self):
        assert sanitize_instance_id("") == "unknown"


class TestRecordIsLive:
    @needs_proc
    def test_matches_current_process(self):
        assert record_is_live(_record(_SELF_START)) is True

    @needs_proc
    def test_rejects_reused_pid(self):
        assert record_is_live(_record(123)) is False


class TestRunRegistry:
    def test_get_returns_sole_record(self, tmp_path):
        registry = RunRegistry(tmp_path / "email.runs.json")
        registry.replace(_record(_SELF_START))
        got = registry.get()
        assert got is not None and got.run_id == "deadbeef"

    def test_get_prunes_stale_record(self, tmp_path):
        registry = RunRegistry(tmp_path / "email.runs.json")
        registry.replace(_record(123))
        assert registry.get() is None
        assert not registry.path.exists()

    def test_remove_is_idempotent(self, tmp_path):
        registry = RunRegistry(tmp_path / "email.runs.json")
        registry.replace(_record(_SELF_START))
        registry.remove()
        registry.remove()
        assert registry.get() is None


class TestResolveRun:
    def _patch(self, monkeypatch, tmp_path, populate):
        reg = RunRegistry(tmp_path / "email.runs.json")
        populate(reg)
        monkeypatch.setattr(cli_bot, "get_settings", lambda: types.SimpleNamespace())
        monkeypatch.setattr(cli_bot, "setup_logging", lambda *a, **k: None)
        monkeypatch.setattr(cli_bot, "runs_registry", lambda iid, s: reg)
        return reg

    @needs_proc
    def test_returns_the_requested_run(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, tmp_path, lambda reg: reg.replace(_record(_SELF_START)))
        got = cli_bot.resolve_run("email", "deadbeef")
        assert got.pid == os.getpid()

    def test_errors_on_mismatched_run_id(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, tmp_path, lambda reg: reg.replace(_record(_SELF_START)))
        with pytest.raises(typer.Exit):
            cli_bot.resolve_run("email", "nope")

    def test_errors_when_no_active_runs(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, tmp_path, lambda reg: None)
        with pytest.raises(typer.Exit):
            cli_bot.resolve_run("email", "deadbeef")


class TestPidHelpers:
    def test_nonpositive_pid_is_dead(self):
        assert runs_mod.pid_alive(0) is False

    def test_missing_process_is_dead(self, monkeypatch):
        def missing(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(runs_mod.os, "kill", missing)
        assert runs_mod.pid_alive(123) is False

    def test_permission_error_means_alive(self, monkeypatch):
        def denied(pid, sig):
            raise PermissionError

        monkeypatch.setattr(runs_mod.os, "kill", denied)
        assert runs_mod.pid_alive(123) is True

    def test_other_oserror_is_dead(self, monkeypatch):
        def broken(pid, sig):
            raise OSError

        monkeypatch.setattr(runs_mod.os, "kill", broken)
        assert runs_mod.pid_alive(123) is False

    def test_successful_signal_zero_means_alive(self, monkeypatch):
        monkeypatch.setattr(runs_mod.os, "kill", lambda pid, sig: None)
        assert runs_mod.pid_alive(123) is True

    def test_proc_start_time_missing_process(self):
        assert runs_mod.proc_start_time(-1) is None

    def test_record_without_start_tick_uses_pid_liveness(self, monkeypatch):
        monkeypatch.setattr(runs_mod, "pid_alive", lambda pid: True)
        record = _record(None)
        assert runs_mod.record_is_live(record) is True

    def test_record_with_unreadable_start_tick_falls_back_to_pid(self, monkeypatch):
        monkeypatch.setattr(runs_mod, "proc_start_time", lambda pid: None)
        monkeypatch.setattr(runs_mod, "pid_alive", lambda pid: True)
        assert runs_mod.record_is_live(_record(123)) is True


class TestRunRegistryCorruption:
    def test_invalid_json_returns_none(self, tmp_path):
        registry = RunRegistry(tmp_path / "email.runs.json")
        registry.path.write_text("{not-json")
        assert registry.get() is None
        assert registry.path.exists()

    def test_non_object_json_is_ignored(self, tmp_path):
        registry = RunRegistry(tmp_path / "email.runs.json")
        registry.path.write_text("[1, 2, 3]")
        assert registry.load() is None
        assert registry.get() is None

    def test_default_runs_path_uses_data_folder(self):
        assert (
            runs_mod.runs_path(None, "email-alice@ex.com")
            == runs_mod.Path("data") / "email-alice@ex.com.runs.json"
        )
