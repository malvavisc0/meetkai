"""Tests for application-layer credential encryption (kai.cockpit.secrets)."""

import pytest

from kai.cockpit.secrets import (
    decrypt,
    decrypt_config,
    encrypt,
    encrypt_config,
    is_encrypted,
)

_KEY = "a" * 64  # 32 hex bytes


@pytest.fixture(autouse=True)
def _encryption_env(monkeypatch, tmp_path):
    # Chdir to tmp so EncryptionSettings (env_file=".env") doesn't read the
    # real .env — pydantic-settings reads from CWD's .env, which would leak
    # the production KAI_CREDENTIAL_KEY_VERSION into the test.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAI_CREDENTIAL_ENCRYPTION_KEY", _KEY)
    monkeypatch.setenv("KAI_CREDENTIAL_KEY_VERSION", "v1")
    from kai.cockpit import secrets as secrets_mod

    secrets_mod._clear_key_cache()
    yield
    secrets_mod._clear_key_cache()


class TestRoundTrip:
    def test_encrypt_decrypt_round_trip(self):
        plaintext = "postgresql://user:s3cret@host:5432/db"
        ciphertext = encrypt(plaintext)
        assert ciphertext != plaintext
        assert decrypt(ciphertext) == plaintext

    def test_ciphertext_starts_with_version(self):
        ciphertext = encrypt("secret")
        assert ciphertext.startswith("v1:")

    def test_changing_version_changes_prefix(self, monkeypatch):
        ciphertext_v1 = encrypt("secret")
        assert ciphertext_v1.startswith("v1:")

        from kai.cockpit import secrets as secrets_mod

        monkeypatch.setenv("KAI_CREDENTIAL_KEY_VERSION", "v2")
        secrets_mod._clear_key_cache()
        ciphertext_v2 = encrypt("secret")
        assert ciphertext_v2.startswith("v2:")

    def test_old_version_decrypts_after_version_bump(self, monkeypatch):
        ciphertext = encrypt("secret-value")
        from kai.cockpit import secrets as secrets_mod

        monkeypatch.setenv("KAI_CREDENTIAL_KEY_VERSION", "v2")
        secrets_mod._clear_key_cache()
        assert decrypt(ciphertext) == "secret-value"


class TestDecryptErrors:
    def test_tampered_ciphertext_raises_value_error(self):
        ciphertext = encrypt("secret")
        tampered = ciphertext[:-4] + "xxxx"
        with pytest.raises(ValueError, match="decryption failed"):
            decrypt(tampered)

    def test_plaintext_raises_value_error(self):
        with pytest.raises(ValueError, match="missing version envelope"):
            decrypt("not-a-ciphertext")


class TestConfigHelpers:
    def test_encrypt_config_encrypts_secret_fields_only(self):
        config = {"url": "postgresql://user:pw@host/db", "label": "prod"}
        encrypted = encrypt_config("database", config)
        assert is_encrypted(encrypted["url"])
        assert not is_encrypted(encrypted["label"])
        assert encrypted["label"] == "prod"

    def test_decrypt_config_reverses_encrypt(self):
        config = {"url": "postgresql://user:pw@host/db", "label": "prod"}
        encrypted = encrypt_config("database", config)
        decrypted = decrypt_config("database", encrypted)
        assert decrypted["url"] == "postgresql://user:pw@host/db"
        assert decrypted["label"] == "prod"

    def test_encrypt_config_idempotent_on_already_encrypted(self):
        config = {"url": "postgresql://user:pw@host/db", "label": "prod"}
        once = encrypt_config("database", config)
        twice = encrypt_config("database", once)
        assert once["url"] == twice["url"]

    def test_encrypt_config_rejects_non_credential_type(self):
        with pytest.raises(ValueError, match="not a credential connection type"):
            encrypt_config("whatsapp", {"waha_session": "x"})


class TestMissingKey:
    def test_encrypt_without_key_raises_runtime_error(self, monkeypatch):
        monkeypatch.delenv("KAI_CREDENTIAL_ENCRYPTION_KEY")
        from kai.cockpit import secrets as secrets_mod

        secrets_mod._clear_key_cache()
        with pytest.raises(RuntimeError, match="KAI_CREDENTIAL_ENCRYPTION_KEY"):
            encrypt("secret")

    def test_encrypt_without_version_raises_runtime_error(self, monkeypatch):
        monkeypatch.delenv("KAI_CREDENTIAL_KEY_VERSION")
        from kai.cockpit import secrets as secrets_mod

        secrets_mod._clear_key_cache()
        with pytest.raises(RuntimeError, match="KAI_CREDENTIAL_KEY_VERSION"):
            encrypt("secret")


class TestIsEncrypted:
    def test_encrypted_value_is_true(self):
        assert is_encrypted(encrypt("secret")) is True

    def test_plaintext_is_false(self):
        assert is_encrypted("postgresql://user:pw@host/db") is False

    def test_empty_string_is_false(self):
        assert is_encrypted("") is False

    def test_short_v_prefix_is_false(self):
        assert is_encrypted("v1:short") is False
