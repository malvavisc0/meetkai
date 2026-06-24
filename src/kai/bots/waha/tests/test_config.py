import pytest
from pydantic import ValidationError

from kai.bots.waha.config import WahaSettings


class TestWahaSettingsValidation:
    def test_valid_defaults(self):
        s = WahaSettings(_env_file="")
        assert s.url == "http://localhost:3000"
        assert s.session == "default"
        assert s.webhook_port == 8000
        assert s.webhook_path == "/webhook/waha"

    def test_url_must_be_http(self):
        with pytest.raises(ValidationError, match="http"):
            WahaSettings(url="ftp://example.com", _env_file="")

    def test_url_no_host(self):
        with pytest.raises(ValidationError, match="host"):
            WahaSettings(url="http://", _env_file="")

    def test_url_strips_trailing_slash(self):
        s = WahaSettings(url="http://example.com/", _env_file="")
        assert s.url == "http://example.com"

    def test_webhook_port_too_low(self):
        with pytest.raises(ValidationError, match="1-65535"):
            WahaSettings(webhook_port=0, _env_file="")

    def test_webhook_port_too_high(self):
        with pytest.raises(ValidationError, match="1-65535"):
            WahaSettings(webhook_port=70000, _env_file="")

    def test_webhook_port_valid_range(self):
        s = WahaSettings(webhook_port=1, _env_file="")
        assert s.webhook_port == 1
        s = WahaSettings(webhook_port=65535, _env_file="")
        assert s.webhook_port == 65535

    def test_webhook_path_must_start_with_slash(self):
        with pytest.raises(ValidationError, match="start with /"):
            WahaSettings(webhook_path="webhook", _env_file="")

    def test_session_cannot_be_empty(self):
        with pytest.raises(ValidationError, match="empty"):
            WahaSettings(session="", _env_file="")

    def test_session_strips_whitespace(self):
        s = WahaSettings(session="  default  ", _env_file="")
        assert s.session == "default"


class TestWahaValidateStartup:
    def test_warns_on_missing_api_key(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="kai.bots.waha.config"):
            s = WahaSettings(_env_file="", api_key="")
            warnings = s.validate_startup()
            assert len(warnings) == 1
            assert "WAHA_API_KEY" in warnings[0]

    def test_no_warnings_when_configured(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="kai.bots.waha.config"):
            s = WahaSettings(_env_file="", api_key="real-key")
            warnings = s.validate_startup()
            assert len(warnings) == 0
