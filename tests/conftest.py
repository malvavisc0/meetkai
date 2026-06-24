import pytest

from kai.config.settings import Settings


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
