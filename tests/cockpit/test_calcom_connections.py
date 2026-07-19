"""Tests for the CalcomConnectionsService."""

import httpx
import pytest

from kai.cockpit.connections.secrets import decrypt_config, is_encrypted

_KEY = "a" * 64


def _fake_resp(status_code: int = 200):
    class _FakeResp:
        status_code: int

    r = _FakeResp()
    r.status_code = status_code
    return r


@pytest.fixture(autouse=True)
def _encryption_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAI_CREDENTIAL_ENCRYPTION_KEY", _KEY)
    monkeypatch.setenv("KAI_CREDENTIAL_KEY_VERSION", "v1")
    from kai.cockpit.connections import secrets

    secrets._clear_key_cache()

    # save() probes Cal.com's GET /v2/me to verify the key. Mock the HTTP
    # call so tests don't hit the network. Individual tests override
    # httpx.get to exercise other status codes.
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _fake_resp(200))
    yield
    secrets._clear_key_cache()


class TestSave:
    def test_save_encrypts_api_key(self, db, user):
        from kai.cockpit.connections.calcom import CalcomConnectionsService

        svc = CalcomConnectionsService(db)
        conn = svc.save(user, api_key="cal_live_abc123", base_url="")
        assert conn.config.get("api_key") != "cal_live_abc123"
        assert is_encrypted(conn.config["api_key"])

    def test_save_sets_status_connected_on_200(self, db, user):
        from kai.cockpit.connections.calcom import CalcomConnectionsService

        svc = CalcomConnectionsService(db)
        conn = svc.save(user, api_key="cal_live_abc123", base_url="")
        assert conn.status == "connected"

    def test_save_empty_api_key_preserves_existing(self, db, user):
        from kai.cockpit.connections.calcom import CalcomConnectionsService

        svc = CalcomConnectionsService(db)
        svc.save(user, api_key="cal_live_abc123", base_url="https://api.cal.com/v2")
        conn = svc.save(user, api_key="", base_url="https://custom.example.com/v2")
        assert conn.config.get("base_url") == "https://custom.example.com/v2"
        assert is_encrypted(conn.config["api_key"])
        assert decrypt_config("calcom", conn.config)["api_key"] == "cal_live_abc123"

    def test_save_auth_rejection_marks_disconnected(self, db, user, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _fake_resp(401))
        from kai.cockpit.connections.calcom import CalcomConnectionsService

        svc = CalcomConnectionsService(db)
        conn = svc.save(user, api_key="cal_live_bad", base_url="")
        assert conn.status == "disconnected"

    def test_save_transient_failure_preserves_prior_status(self, db, user, monkeypatch):
        import httpx

        # First save succeeds (status=connected via the autouse 200 mock).
        from kai.cockpit.connections.calcom import CalcomConnectionsService

        svc = CalcomConnectionsService(db)
        svc.save(user, api_key="cal_live_abc123", base_url="")
        # A subsequent save that hits a 500 must preserve "connected".
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _fake_resp(500))
        conn = svc.save(user, api_key="cal_live_abc123", base_url="")
        assert conn.status == "connected"

    def test_save_network_error_is_transient(self, db, user, monkeypatch):
        import httpx

        def _raise(*a, **kw):
            raise httpx.ConnectError("boom")

        monkeypatch.setattr(httpx, "get", _raise)
        from kai.cockpit.connections.calcom import CalcomConnectionsService

        svc = CalcomConnectionsService(db)
        # No prior connection, so a transient failure lands on "disconnected"
        # (nothing to preserve) — but it must not raise.
        conn = svc.save(user, api_key="cal_live_abc123", base_url="")
        assert conn.status == "disconnected"


class TestDecryptApiKey:
    def test_round_trips(self, db, user):
        from kai.cockpit.connections.calcom import CalcomConnectionsService

        svc = CalcomConnectionsService(db)
        svc.save(user, api_key="cal_live_abc123", base_url="")
        assert svc.decrypt_api_key(user) == "cal_live_abc123"

    def test_none_when_no_connection(self, db, user):
        from kai.cockpit.connections.calcom import CalcomConnectionsService

        svc = CalcomConnectionsService(db)
        assert svc.decrypt_api_key(user) is None


class TestProbe:
    def test_403_is_non_transient(self, db, user, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _fake_resp(403))
        from kai.cockpit.connections.calcom import CalcomConnectionsService

        svc = CalcomConnectionsService(db)
        ok, _, transient = svc._probe("k", "")
        assert ok is False
        assert transient is False

    def test_429_is_transient(self, db, user, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _fake_resp(429))
        from kai.cockpit.connections.calcom import CalcomConnectionsService

        svc = CalcomConnectionsService(db)
        ok, _, transient = svc._probe("k", "")
        assert ok is False
        assert transient is True

    def test_blank_base_url_uses_default(self, db, user, monkeypatch):
        captured = {}

        def _fake_get(url, **kw):
            captured["url"] = url
            return _fake_resp(200)

        monkeypatch.setattr(httpx, "get", _fake_get)
        from kai.cockpit.connections.calcom import CalcomConnectionsService

        svc = CalcomConnectionsService(db)
        svc._probe("k", "")
        assert captured["url"] == "https://api.cal.com/v2/me"


class TestTest:
    def test_adhoc_key(self, db, user, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _fake_resp(200))
        from kai.cockpit.connections.calcom import CalcomConnectionsService

        svc = CalcomConnectionsService(db)
        ok, msg = svc.test(user, api_key="cal_live_adhoc", base_url="")
        assert ok is True
        assert msg == "ok"

    def test_no_key_returns_false(self, db, user):
        from kai.cockpit.connections.calcom import CalcomConnectionsService

        svc = CalcomConnectionsService(db)
        ok, msg = svc.test(user)
        assert ok is False
        assert "no Cal.com API key" in msg


class TestDelete:
    def test_delete_removes_row(self, db, user):
        from kai.cockpit.connections.calcom import CalcomConnectionsService

        svc = CalcomConnectionsService(db)
        svc.save(user, api_key="cal_live_abc123", base_url="")
        assert svc.get(user) is not None
        svc.delete(user)
        assert svc.get(user) is None

    def test_delete_when_none_is_noop(self, db, user):
        from kai.cockpit.connections.calcom import CalcomConnectionsService

        svc = CalcomConnectionsService(db)
        svc.delete(user)
        assert svc.get(user) is None
