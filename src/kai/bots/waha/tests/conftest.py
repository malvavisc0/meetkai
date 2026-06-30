import os

import pytest


@pytest.fixture(autouse=True)
def _clean_kai_env(monkeypatch):
    """Clear KAI_* environment variables so WAHA settings tests see defaults.

    pydantic-settings reads os.environ, and ``_env_file=""`` only disables
    .env *file* loading — it does NOT stop real exported env vars from
    overriding Field defaults. A shell that sourced the project .env (or a
    prior ``kai start``) leaks KAI_WAHA_URL etc. into the test process.
    """
    for key in list(os.environ):
        if key.startswith("KAI_"):
            monkeypatch.delenv(key, raising=False)
