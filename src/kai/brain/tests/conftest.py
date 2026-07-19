import os

import pytest

from kai.brain.config import BrainSettings


@pytest.fixture(autouse=True)
def _clean_brain_env(monkeypatch):
    """Clear KAI_BRAIN_* env vars so config tests see real Field defaults.

    pydantic-settings reads os.environ, and ``_env_file=None`` only disables
    .env *file* loading — it does not stop real exported env vars (a shell
    that sourced .env, or a running ``kai start``) from overriding the Field
    defaults. Without this, ``BrainSettings.for_test()`` picks up the
    leaked base_url / api_key and the "defaults are empty" assertions fail.
    """
    for key in list(os.environ):
        if key.startswith("KAI_BRAIN_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def settings():
    return BrainSettings.for_test(
        base_url="http://localhost:8000",
        morphik_token="morphik-test-token",
        workspace="kai-test",
        crawler_url="http://localhost:11235",
        crawl4ai_token="crawl4ai-test-token",
    )
