import pytest
from pydantic import ValidationError

from kai.config.settings import Settings


class TestSettingsValidation:
    def test_valid_defaults(self):
        s = Settings(
            _env_file="",
            llm_api_key="test-key",
        )
        assert s.llm_api_base == "https://api.openai.com/v1"
        assert s.llm_model == "gpt-4o-mini"
        assert s.agent_max_history_messages == 100

    def test_llm_api_base_must_be_http(self):
        with pytest.raises(ValidationError, match="http"):
            Settings(llm_api_base="ftp://example.com", _env_file="")

    def test_llm_api_base_strips_trailing_slash(self):
        s = Settings(llm_api_base="http://example.com/v1/", _env_file="")
        assert s.llm_api_base == "http://example.com/v1"

    def test_agent_max_history_messages_must_be_positive(self):
        with pytest.raises(ValidationError, match=">= 0"):
            Settings(agent_max_history_messages=-1, _env_file="")

    def test_agent_max_history_chars_must_be_positive(self):
        with pytest.raises(ValidationError, match=">= 0"):
            Settings(agent_max_history_chars=-1, _env_file="")


class TestValidateStartup:
    def test_warns_on_missing_api_key(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="kai.config.settings"):
            s = Settings(
                _env_file="",
                llm_api_key="",
            )
            warnings = s.validate_startup()
            assert len(warnings) == 1
            assert "LLM_API_KEY" in warnings[0]

    def test_warns_on_placeholder_api_key(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="kai.config.settings"):
            s = Settings(
                _env_file="",
                llm_api_key="sk-placeholder",
            )
            warnings = s.validate_startup()
            assert len(warnings) == 1
            assert "LLM_API_KEY" in warnings[0]

    def test_no_warnings_when_configured(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="kai.config.settings"):
            s = Settings(
                _env_file="",
                llm_api_key="real-key",
            )
            warnings = s.validate_startup()
            assert len(warnings) == 0
