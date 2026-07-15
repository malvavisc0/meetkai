"""Tests for the EmailConnectionsService (05-ui / 06-tests).

Covers: ``save()`` encrypts the signing secret + API key and probes
the credentials (signing-secret base64 validity + API key accepted by
Resend's ``GET /domains``), setting status="connected" on success,
``delete()`` round-trip, ``decrypt_secret()``/``decrypt_api_key()``
round-trip, empty-secret preserve-existing, and the start gate treating
the ingress-only connection as connected only when both secret fields
are present.
"""

from __future__ import annotations

import pytest

from kai.cockpit.secrets import decrypt_config, is_encrypted

_KEY = "a" * 64


@pytest.fixture(autouse=True)
def _encryption_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAI_CREDENTIAL_ENCRYPTION_KEY", _KEY)
    monkeypatch.setenv("KAI_CREDENTIAL_KEY_VERSION", "v1")
    from kai.cockpit import secrets

    secrets._clear_key_cache()

    # save() now probes Resend's API (GET /domains) to verify the key.
    # Mock the HTTP call so tests don't hit the network. Returns 200 =
    # key accepted, so save() marks the connection "connected".
    import httpx

    class _FakeResp:
        status_code = 200

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResp())
    yield
    secrets._clear_key_cache()


class TestSave:
    def test_save_encrypts_signing_secret_and_api_key(self, db, user):
        from kai.cockpit.email_connections import EmailConnectionsService

        svc = EmailConnectionsService(db)
        conn = svc.save(user, signing_secret="whsec_dGVzdA==", api_key="re_live_abc123")
        assert conn.config.get("signing_secret") != "whsec_dGVzdA=="
        assert conn.config.get("api_key") != "re_live_abc123"
        assert is_encrypted(conn.config["signing_secret"])
        assert is_encrypted(conn.config["api_key"])

    def test_save_sets_status_connected(self, db, user):
        from kai.cockpit.email_connections import EmailConnectionsService

        svc = EmailConnectionsService(db)
        conn = svc.save(user, signing_secret="whsec_dGVzdA==", api_key="re_live_abc123")
        assert conn.status == "connected"

    def test_save_empty_secret_preserves_existing(self, db, user):
        from kai.cockpit.email_connections import EmailConnectionsService

        svc = EmailConnectionsService(db)
        svc.save(user, signing_secret="whsec_dGVzdA==", api_key="re_live_abc123")
        conn = svc.save(user, signing_secret="", api_key="")
        # Both secrets are still the encrypted originals, not wiped
        assert is_encrypted(conn.config["signing_secret"])
        assert is_encrypted(conn.config["api_key"])
        decrypted = decrypt_config("resend", conn.config)
        assert decrypted["signing_secret"] == "whsec_dGVzdA=="
        assert decrypted["api_key"] == "re_live_abc123"

    def test_save_new_secret_overwrites(self, db, user):
        from kai.cockpit.email_connections import EmailConnectionsService

        svc = EmailConnectionsService(db)
        svc.save(user, signing_secret="old-secret", api_key="old-key")
        conn = svc.save(user, signing_secret="new-secret", api_key="new-key")
        decrypted = decrypt_config("resend", conn.config)
        assert decrypted["signing_secret"] == "new-secret"
        assert decrypted["api_key"] == "new-key"

    def test_save_updates_only_provided_field(self, db, user):
        """Updating just the signing secret must not clobber a stored api_key."""
        from kai.cockpit.email_connections import EmailConnectionsService

        svc = EmailConnectionsService(db)
        svc.save(user, signing_secret="old-secret", api_key="stays-the-same")
        conn = svc.save(user, signing_secret="new-secret", api_key="")
        decrypted = decrypt_config("resend", conn.config)
        assert decrypted["signing_secret"] == "new-secret"
        assert decrypted["api_key"] == "stays-the-same"


class TestDelete:
    def test_delete_removes_connection(self, db, user):
        from kai.cockpit.email_connections import EmailConnectionsService

        svc = EmailConnectionsService(db)
        svc.save(user, signing_secret="whsec_dGVzdA==", api_key="re_live_abc123")
        assert svc.get(user) is not None

        svc.delete(user)
        assert svc.get(user) is None

    def test_delete_when_no_connection_is_noop(self, db, user):
        from kai.cockpit.email_connections import EmailConnectionsService

        svc = EmailConnectionsService(db)
        svc.delete(user)  # should not raise


class TestDecryptSecret:
    def test_decrypt_secret_round_trips(self, db, user):
        from kai.cockpit.email_connections import EmailConnectionsService

        svc = EmailConnectionsService(db)
        svc.save(user, signing_secret="whsec_dGVzdA==", api_key="re_live_abc123")
        assert svc.decrypt_secret(user) == "whsec_dGVzdA=="

    def test_decrypt_secret_none_when_no_connection(self, db, user):
        from kai.cockpit.email_connections import EmailConnectionsService

        svc = EmailConnectionsService(db)
        assert svc.decrypt_secret(user) is None

    def test_decrypt_api_key_round_trips(self, db, user):
        from kai.cockpit.email_connections import EmailConnectionsService

        svc = EmailConnectionsService(db)
        svc.save(user, signing_secret="whsec_dGVzdA==", api_key="re_live_abc123")
        assert svc.decrypt_api_key(user) == "re_live_abc123"

    def test_decrypt_api_key_none_when_no_connection(self, db, user):
        from kai.cockpit.email_connections import EmailConnectionsService

        svc = EmailConnectionsService(db)
        assert svc.decrypt_api_key(user) is None


class TestSecretEncryptedAtRest:
    def test_secret_stays_encrypted_after_save(self, db, user):
        from kai.cockpit.email_connections import EmailConnectionsService

        svc = EmailConnectionsService(db)
        svc.save(user, signing_secret="whsec_dGVzdA==", api_key="re_live_abc123")
        conn = svc.get(user)
        assert conn is not None
        # The raw config column has ciphertext, not plaintext
        assert "whsec_dGVzdA==" not in str(conn.config)
        assert "re_live_abc123" not in str(conn.config)
        assert is_encrypted(conn.config["signing_secret"])
        assert is_encrypted(conn.config["api_key"])


class TestStartGate:
    def test_start_gate_treats_connected_resend_as_connected(self, db, user):
        from kai.cockpit.deployments import _is_connected
        from kai.cockpit.email_connections import EmailConnectionsService
        from kai.cockpit.models import Connection
        from kai.cockpit.smtp_connections import SmtpConnectionsService

        # Connect resend + smtp (both required for the email bot)
        EmailConnectionsService(db).save(user, signing_secret="dGVzdA==", api_key="re_test")
        SmtpConnectionsService(db).save(
            user,
            host="smtp.example.com",
            port=587,
            username="user",
            password="pass",
            from_address="support@meetk.ai",
        )

        resend_conn = (
            db.query(Connection)
            .filter(Connection.user_id == user.id, Connection.service == "resend")
            .first()
        )
        assert _is_connected("resend", resend_conn) is True

    def test_start_gate_rejects_resend_missing_api_key(self, db, user):
        """Signing secret alone isn't enough — api_key is required to fetch content."""
        from kai.cockpit.deployments import _is_connected
        from kai.cockpit.email_connections import EmailConnectionsService
        from kai.cockpit.models import Connection

        EmailConnectionsService(db).save(user, signing_secret="dGVzdA==", api_key="")

        resend_conn = (
            db.query(Connection)
            .filter(Connection.user_id == user.id, Connection.service == "resend")
            .first()
        )
        assert _is_connected("resend", resend_conn) is False
