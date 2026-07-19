"""Tests for the DatabaseConnectionsService (Fix 05)."""

import pytest


@pytest.fixture(autouse=True)
def _encryption_env(monkeypatch):
    """Provide the encryption env vars the secrets module requires."""
    monkeypatch.setenv("KAI_CREDENTIAL_ENCRYPTION_KEY", "a" * 64)
    monkeypatch.setenv("KAI_CREDENTIAL_KEY_VERSION", "v1")
    # Clear the key cache so the new env takes effect.
    from kai.cockpit.connections import secrets

    secrets._clear_key_cache()
    yield
    secrets._clear_key_cache()


class TestSave:
    def test_save_encrypts_url(self, db, user):
        from kai.cockpit.connections.database import DatabaseConnectionsService

        svc = DatabaseConnectionsService(db)
        conn = svc.save(user, label="prod", url="postgresql://user:pass@host/db")
        assert conn.config.get("label") == "prod"
        assert conn.config.get("url") != "postgresql://user:pass@host/db"
        from kai.cockpit.connections.secrets import is_encrypted

        assert is_encrypted(conn.config["url"])

    def test_save_empty_url_preserves_existing(self, db, user):
        from kai.cockpit.connections.database import DatabaseConnectionsService

        svc = DatabaseConnectionsService(db)
        svc.save(user, label="prod", url="postgresql://user:pass@host/db")
        conn = svc.save(user, label="prod-updated", url="")
        assert conn.config.get("label") == "prod-updated"
        # URL is still there (encrypted, unchanged)
        from kai.cockpit.connections.secrets import is_encrypted

        assert is_encrypted(conn.config["url"])


class TestDecryptUrl:
    def test_round_trips(self, db, user):
        from kai.cockpit.connections.database import DatabaseConnectionsService

        svc = DatabaseConnectionsService(db)
        svc.save(user, label="prod", url="postgresql://user:pass@host/db")
        assert svc.decrypt_url(user) == "postgresql://user:pass@host/db"

    def test_none_when_no_connection(self, db, user):
        from kai.cockpit.connections.database import DatabaseConnectionsService

        svc = DatabaseConnectionsService(db)
        assert svc.decrypt_url(user) is None


class TestTest:
    def test_sqlite_succeeds(self, db, user):
        from kai.cockpit.connections.database import DatabaseConnectionsService

        svc = DatabaseConnectionsService(db)
        svc.save(user, label="test", url="sqlite://")
        ok, msg = svc.test(user)
        assert ok is True
        assert msg == "ok"

    def test_adhoc_url(self, db, user):
        """test() with an explicit url tests that value, not the persisted one."""
        from kai.cockpit.connections.database import DatabaseConnectionsService

        svc = DatabaseConnectionsService(db)
        svc.save(user, label="bad", url="postgresql://nonexistent@localhost/x")
        # Override with a working DSN
        ok, msg = svc.test(user, url="sqlite://")
        assert ok is True
        assert msg == "ok"

    def test_no_url_returns_false(self, db, user):
        from kai.cockpit.connections.database import DatabaseConnectionsService

        svc = DatabaseConnectionsService(db)
        svc.save(user, label="empty", url="")
        ok, msg = svc.test(user)
        assert ok is False
        assert "no connection URL" in msg


class TestDelete:
    def test_delete_removes_row(self, db, user):
        from kai.cockpit.connections.database import DatabaseConnectionsService

        svc = DatabaseConnectionsService(db)
        svc.save(user, label="prod", url="sqlite://")
        assert svc.get(user) is not None
        svc.delete(user)
        assert svc.get(user) is None

    def test_delete_when_none_is_noop(self, db, user):
        from kai.cockpit.connections.database import DatabaseConnectionsService

        svc = DatabaseConnectionsService(db)
        svc.delete(user)
        assert svc.get(user) is None
