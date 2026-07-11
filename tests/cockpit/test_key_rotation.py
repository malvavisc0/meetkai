"""Tests for on-demand credential key rotation (kai.cockpit.key_rotation)."""

import secrets

import pytest

from kai.cockpit.key_rotation import _update_env_file, rotate_credential_key
from kai.cockpit.models import Connection
from kai.cockpit.secrets import decrypt_config, encrypt_config, is_encrypted

_KEY = secrets.token_hex(32)


@pytest.fixture(autouse=True)
def _encryption_env(monkeypatch, tmp_path):
    # Chdir to tmp so EncryptionSettings (env_file=".env") and
    # _update_env_file (Path(".env")) don't touch the real .env.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAI_CREDENTIAL_ENCRYPTION_KEY", _KEY)
    monkeypatch.setenv("KAI_CREDENTIAL_KEY_VERSION", "v1")
    from kai.cockpit import secrets as secrets_mod

    secrets_mod._clear_key_cache()
    yield
    secrets_mod._clear_key_cache()


def _db_conn(user_id: int, service: str, config: dict) -> Connection:
    return Connection(
        user_id=user_id,
        service=service,
        status="connected",
        config=config,
        created_at="now",
        updated_at="now",
    )


class TestRotateCredentialKey:
    def test_bumps_version_and_reencrypts(self, db, user):
        encrypted_url = encrypt_config("database", {"url": "postgresql://u:p@h/db", "label": "x"})
        conn = _db_conn(user.id, "database", encrypted_url)
        db.add(conn)
        db.commit()

        new_version, _ = rotate_credential_key(db)

        assert new_version == "v2"
        db.refresh(conn)
        assert conn.config["url"].startswith("v2:")
        assert is_encrypted(conn.config["url"])
        assert decrypt_config("database", conn.config)["url"] == "postgresql://u:p@h/db"
        assert conn.config["label"] == "x"

    def test_consecutive_rotations(self, db, user):
        conn = _db_conn(
            user.id, "database", encrypt_config("database", {"url": "secret-url", "label": "p"})
        )
        db.add(conn)
        db.commit()

        v2, _ = rotate_credential_key(db)
        assert v2 == "v2"
        v3, _ = rotate_credential_key(db)
        assert v3 == "v3"

        db.refresh(conn)
        assert conn.config["url"].startswith("v3:")
        assert decrypt_config("database", conn.config)["url"] == "secret-url"

    def test_no_credential_connections(self, db):
        new_version, _ = rotate_credential_key(db)
        assert new_version == "v2"

    def test_multiple_services_reencrypted(self, db, user):
        db_conn = _db_conn(
            user.id, "database", encrypt_config("database", {"url": "db-url", "label": "d"})
        )
        smtp_conn = _db_conn(
            user.id,
            "smtp",
            encrypt_config(
                "smtp",
                {
                    "host": "h",
                    "port": 587,
                    "username": "u",
                    "password": "pw",
                    "from_address": "a@b.c",
                    "use_tls": True,
                },
            ),
        )
        db.add(db_conn)
        db.add(smtp_conn)
        db.commit()

        new_version, _ = rotate_credential_key(db)
        assert new_version == "v2"

        db.refresh(db_conn)
        db.refresh(smtp_conn)
        assert db_conn.config["url"].startswith("v2:")
        assert smtp_conn.config["password"].startswith("v2:")
        assert decrypt_config("database", db_conn.config)["url"] == "db-url"
        assert decrypt_config("smtp", smtp_conn.config)["password"] == "pw"


class TestUpdateEnvFile:
    def test_rewrites_existing_line(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text(
            "KAI_CREDENTIAL_ENCRYPTION_KEY=abc\nKAI_CREDENTIAL_KEY_VERSION=v1\nOTHER=val\n",
            encoding="utf-8",
        )

        written = _update_env_file("v2")

        assert written is True
        text = env_path.read_text(encoding="utf-8")
        assert "KAI_CREDENTIAL_KEY_VERSION=v2" in text
        assert "KAI_CREDENTIAL_ENCRYPTION_KEY=abc" in text
        assert "OTHER=val" in text

    def test_appends_when_absent(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("OTHER=val\n", encoding="utf-8")

        written = _update_env_file("v2")

        assert written is True
        text = env_path.read_text(encoding="utf-8")
        assert "KAI_CREDENTIAL_KEY_VERSION=v2" in text
        assert "OTHER=val" in text

    def test_returns_false_when_no_env_file(self, tmp_path):
        written = _update_env_file("v2")

        assert written is False
