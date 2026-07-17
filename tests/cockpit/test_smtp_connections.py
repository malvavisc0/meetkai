"""Tests for the SmtpConnectionsService (Fix 06)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _encryption_env(monkeypatch):
    """Provide the encryption env vars the secrets module requires."""
    monkeypatch.setenv("KAI_CREDENTIAL_ENCRYPTION_KEY", "a" * 64)
    monkeypatch.setenv("KAI_CREDENTIAL_KEY_VERSION", "v1")
    from kai.cockpit.connections import secrets

    secrets._clear_key_cache()
    yield
    secrets._clear_key_cache()


class TestSave:
    def test_save_encrypts_password(self, db, user):
        from kai.cockpit.connections.smtp import SmtpConnectionsService

        svc = SmtpConnectionsService(db)
        conn = svc.save(
            user,
            host="smtp.example.com",
            port=587,
            username="user",
            password="secret123",
            from_address="user@example.com",
        )
        assert conn.config.get("host") == "smtp.example.com"
        assert conn.config.get("password") != "secret123"
        from kai.cockpit.connections.secrets import is_encrypted

        assert is_encrypted(conn.config["password"])

    def test_save_empty_password_preserves_existing(self, db, user):
        from kai.cockpit.connections.smtp import SmtpConnectionsService

        svc = SmtpConnectionsService(db)
        svc.save(
            user,
            host="smtp.example.com",
            port=587,
            username="user",
            password="secret123",
            from_address="user@example.com",
        )
        conn = svc.save(
            user,
            host="smtp.newhost.com",
            port=2525,
            username="user2",
            password="",
            from_address="user2@example.com",
        )
        assert conn.config.get("host") == "smtp.newhost.com"
        assert conn.config.get("username") == "user2"
        # Password is still there (encrypted, unchanged)
        from kai.cockpit.connections.secrets import is_encrypted

        assert is_encrypted(conn.config["password"])

    def test_save_plaintext_fields_stored(self, db, user):
        from kai.cockpit.connections.smtp import SmtpConnectionsService

        svc = SmtpConnectionsService(db)
        conn = svc.save(
            user,
            host="smtp.example.com",
            port=465,
            username="user",
            password="pw",
            from_address="from@example.com",
            use_tls=False,
        )
        assert conn.config.get("port") == 465
        assert conn.config.get("from_address") == "from@example.com"
        assert conn.config.get("use_tls") is False


class TestDecryptConfig:
    def test_round_trips(self, db, user):
        from kai.cockpit.connections.smtp import SmtpConnectionsService

        svc = SmtpConnectionsService(db)
        svc.save(
            user,
            host="smtp.example.com",
            port=587,
            username="user",
            password="secret123",
            from_address="user@example.com",
        )
        cfg = svc.decrypt_config(user)
        assert cfg["host"] == "smtp.example.com"
        assert cfg["port"] == 587
        assert cfg["username"] == "user"
        assert cfg["password"] == "secret123"
        assert cfg["from_address"] == "user@example.com"
        assert cfg["use_tls"] is True

    def test_none_when_no_connection(self, db, user):
        from kai.cockpit.connections.smtp import SmtpConnectionsService

        svc = SmtpConnectionsService(db)
        assert svc.decrypt_config(user) is None


class TestDelete:
    def test_delete_removes_row(self, db, user):
        from kai.cockpit.connections.smtp import SmtpConnectionsService

        svc = SmtpConnectionsService(db)
        svc.save(
            user,
            host="smtp.example.com",
            port=587,
            username="u",
            password="p",
            from_address="f@example.com",
        )
        assert svc.get(user) is not None
        svc.delete(user)
        assert svc.get(user) is None

    def test_delete_when_none_is_noop(self, db, user):
        from kai.cockpit.connections.smtp import SmtpConnectionsService

        svc = SmtpConnectionsService(db)
        svc.delete(user)
        assert svc.get(user) is None
