import pytest
from pydantic import ValidationError

from kai.bots.waha.config import WahaSettings


class TestWahaSettingsValidation:
    def test_valid_defaults(self):
        s = WahaSettings.for_test(hmac_key="test-secret")
        assert s.url == "http://localhost:3000"
        assert s.session == "default"
        assert s.webhook_port == 8000
        assert s.webhook_path == "/webhook/waha"
        assert s.hmac_key == "test-secret"

    def test_hmac_key_is_required(self):
        # KAI_WAHA_HMAC_KEY is mandatory — construction without it fails.
        with pytest.raises(ValidationError, match="hmac_key"):
            WahaSettings.for_test()

    def test_url_must_be_http(self):
        with pytest.raises(ValidationError, match="http"):
            WahaSettings.for_test(url="ftp://example.com", hmac_key="k")

    def test_url_no_host(self):
        with pytest.raises(ValidationError, match="host"):
            WahaSettings.for_test(url="http://", hmac_key="k")

    def test_url_strips_trailing_slash(self):
        s = WahaSettings.for_test(url="http://example.com/", hmac_key="k")
        assert s.url == "http://example.com"

    def test_webhook_port_too_low(self):
        with pytest.raises(ValidationError, match="1-65535"):
            WahaSettings.for_test(webhook_port=0, hmac_key="k")

    def test_webhook_port_too_high(self):
        with pytest.raises(ValidationError, match="1-65535"):
            WahaSettings.for_test(webhook_port=70000, hmac_key="k")

    def test_webhook_port_valid_range(self):
        s = WahaSettings.for_test(webhook_port=1, hmac_key="k")
        assert s.webhook_port == 1
        s = WahaSettings.for_test(webhook_port=65535, hmac_key="k")
        assert s.webhook_port == 65535

    def test_webhook_path_must_start_with_slash(self):
        with pytest.raises(ValidationError, match="start with /"):
            WahaSettings.for_test(webhook_path="webhook", hmac_key="k")

    def test_session_cannot_be_empty(self):
        with pytest.raises(ValidationError, match="empty"):
            WahaSettings.for_test(session="", hmac_key="k")

    def test_session_strips_whitespace(self):
        s = WahaSettings.for_test(session="  default  ", hmac_key="k")
        assert s.session == "default"


class TestWahaValidateStartup:
    def test_warns_on_missing_api_key(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="kai.bots.waha.config"):
            s = WahaSettings.for_test(api_key="", hmac_key="k")
            warnings = s.validate_startup()
            assert len(warnings) == 1
            assert "WAHA_API_KEY" in warnings[0]

    def test_no_warnings_when_configured(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="kai.bots.waha.config"):
            s = WahaSettings.for_test(api_key="real-key", hmac_key="k")
            warnings = s.validate_startup()
            assert len(warnings) == 0
