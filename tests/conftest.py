import os

import pytest

from kai.config.settings import Settings


@pytest.fixture(autouse=True)
def _clean_kai_env(monkeypatch):
    """Clear KAI_* environment variables so settings tests see real defaults.

    pydantic-settings reads os.environ, and ``_env_file=""`` only disables
    .env *file* loading — it does NOT stop real exported env vars from
    overriding Field defaults. A shell that sourced the project .env (or a
    prior ``kai start``) leaks KAI_LLM_API_BASE / KAI_WAHA_URL into the test
    process, making ``test_valid_defaults`` assert against leaked values
    instead of the intended defaults.

    After clearing, KAI_LOG_DIR is explicitly redirected to /tmp/kai so
    test runs never contaminate the real data/kai/logs/ directory — every
    CLI-driving test (``kai start`` etc.) that calls setup_logging() writes
    its kai.log / ignored_messages.log there instead.
    """
    for key in list(os.environ):
        if key.startswith("KAI_"):
            monkeypatch.delenv(key, raising=False)
    # Redirect logging to a per-worker, per-test-run directory so xdist
    # workers don't interleave writes into /tmp/kai/kai.log. ``worker_id`` is
    # injected by xdist; without xdist it is "master".
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    monkeypatch.setenv("KAI_LOG_DIR", f"/tmp/kai-{worker_id}")

    # setup_logging() is guarded by a module-level _configured flag, so the
    # FIRST call in the process wins and later calls are no-ops. Reset it per
    # test so the /tmp/kai redirect above is actually honoured regardless of
    # whether an earlier test already configured logging against the default
    # data/kai/logs path.
    import kai.logging.logger as logger_mod

    monkeypatch.setattr(logger_mod, "_configured", False)


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    """Ensure tests using KaiAgent(settings=None) never load real history
    from the on-disk data/ folder."""

    def _test_settings() -> Settings:
        return Settings.for_test(agent_history_folder=None)

    monkeypatch.setattr("kai.agent.core.get_settings", _test_settings)
