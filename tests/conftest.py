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
    """
    for key in list(os.environ):
        if key.startswith("KAI_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    """Ensure tests using KaiAgent(settings=None) never load real history
    from the on-disk data/ folder."""

    def _test_settings() -> Settings:
        return Settings(
            _env_file=None,
            agent_history_folder=None,
        )

    monkeypatch.setattr("kai.agent.core.get_settings", _test_settings)
