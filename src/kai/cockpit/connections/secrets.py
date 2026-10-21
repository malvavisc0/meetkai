"""Application-layer encryption for credential Connection secrets.

Provides Fernet symmetric authenticated encryption, uniform across SQLite
dev and Postgres prod (stored as a string in the existing JSON column — no
schema change, no DB-native column type).

The encryption key is derived from ``KAI_CREDENTIAL_ENCRYPTION_KEY`` (a
deployment-wide root secret) via a versioned HKDF context. The version tag
lives inside the ciphertext string (``"v1:gAAAAA..."``) so a rotation can
re-encrypt every row in place without a schema migration or a lookup table.

Only the fields declared in ``CREDENTIAL_TYPES[...].secret_fields`` (or
``WEBHOOK_CONNECTION_TYPES``) are encrypted; everything else in
``Connection.config`` stays plaintext.
"""

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic_settings import BaseSettings, SettingsConfigDict

HKDF_INFO = b"kai-connection-secrets"
VERSION_SEP = ":"


class KeyState:
    """Process-wide state for credential key rotation, encapsulated (no globals)."""

    def __init__(self) -> None:
        # Active key version for new encrypt() calls, set by key rotation.
        # ``None`` means read from ``KAI_CREDENTIAL_KEY_VERSION`` on each call.
        self.active_version_override: str | None = None
        self.key_cache: dict[str, Fernet] = {}

    def clear(self) -> None:
        """Drop cached Fernet instances and reset the active-version override."""
        self.key_cache.clear()
        self.active_version_override = None


KEY_STATE = KeyState()


class EncryptionSettings(BaseSettings):
    """Root secret + active key version for credential encryption."""

    model_config = SettingsConfigDict(env_prefix="KAI_", env_file=".env", extra="ignore")

    credential_encryption_key: str = ""
    credential_key_version: str = ""


def get_encryption_settings() -> EncryptionSettings:
    """Load credential-encryption settings from the environment."""
    return EncryptionSettings()


def root_key_material() -> bytes:
    """Return the root key bytes, failing when the secret is unset."""
    settings = get_encryption_settings()
    if not settings.credential_encryption_key:
        raise RuntimeError(
            "KAI_CREDENTIAL_ENCRYPTION_KEY is not set — required for credential "
            "encryption. Generate with: openssl rand -hex 32"
        )
    return settings.credential_encryption_key.encode()


def active_version() -> str:
    """Return the key version used for new encryptions."""
    if KEY_STATE.active_version_override is not None:
        return KEY_STATE.active_version_override
    settings = get_encryption_settings()
    if not settings.credential_key_version:
        raise RuntimeError(
            "KAI_CREDENTIAL_KEY_VERSION is not set — required for credential "
            "encryption. Set it to the active key version (e.g. v1) in .env."
        )
    return settings.credential_key_version


def set_active_version(version: str) -> None:
    """Set the active encryption key version for subsequent ``encrypt()`` calls."""
    KEY_STATE.active_version_override = version


def derive_fernet(version: str) -> Fernet:
    """Derive (and cache) the Fernet key for a given version."""
    if version in KEY_STATE.key_cache:
        return KEY_STATE.key_cache[version]
    info = HKDF_INFO + b":" + version.encode()
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=info,
    )
    raw = hkdf.derive(root_key_material())
    fernet = Fernet(base64.urlsafe_b64encode(raw))
    KEY_STATE.key_cache[version] = fernet
    return fernet


def clear_key_cache() -> None:
    """Drop cached Fernet instances and reset the active-version override.

    Test/rotation use only.
    """
    KEY_STATE.clear()


def encrypt(plaintext: str) -> str:
    """Encrypt a secret, returning a versioned ciphertext envelope."""
    version = active_version()
    fernet = derive_fernet(version)
    token = fernet.encrypt(plaintext.encode()).decode()
    return f"{version}{VERSION_SEP}{token}"


def decrypt(ciphertext: str) -> str:
    """Decrypt a versioned ciphertext envelope. Raises ValueError on failure."""
    if VERSION_SEP not in ciphertext:
        raise ValueError("ciphertext missing version envelope")
    version, token = ciphertext.split(VERSION_SEP, 1)
    fernet = derive_fernet(version)
    try:
        return fernet.decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("credential decryption failed (wrong key or tampered)") from exc


def is_encrypted(value: str) -> bool:
    """True if value looks like a versioned ciphertext envelope.

    A heuristic, not a guarantee — ``decrypt`` is the real authority (it
    raises on anything invalid). Used by ``encrypt_config``/``decrypt_config``
    to avoid double-encrypting or passing plaintext through.
    """
    if VERSION_SEP not in value:
        return False
    version, _ = value.split(VERSION_SEP, 1)
    return version.startswith("v") and len(value) > len(version) + 10


def encrypt_config(service: str, config: dict) -> dict:
    """Return a copy of config with that service's secret fields encrypted.

    Raises ``ValueError`` for connection types in neither
    ``CREDENTIAL_TYPES`` nor ``WEBHOOK_CONNECTION_TYPES``. Already-encrypted
    fields are left untouched so a re-encrypt is idempotent.
    """
    from kai.cockpit.bots import CREDENTIAL_TYPES, WEBHOOK_CONNECTION_TYPES

    ct = CREDENTIAL_TYPES.get(service) or WEBHOOK_CONNECTION_TYPES.get(service)
    if ct is None:
        raise ValueError(f"{service!r} is not a known connection type")
    out = dict(config)
    for field in ct.secret_fields:
        if field in out and out[field] and not is_encrypted(str(out[field])):
            out[field] = encrypt(str(out[field]))
    return out


def decrypt_config(service: str, config: dict) -> dict:
    """Return a copy of config with secret fields decrypted.

    For use on read paths that need the plaintext (bot subprocess env
    injection, outbound calls). Never use this for template rendering —
    templates render the masked placeholder.
    """
    from kai.cockpit.bots import CREDENTIAL_TYPES, WEBHOOK_CONNECTION_TYPES

    ct = CREDENTIAL_TYPES.get(service) or WEBHOOK_CONNECTION_TYPES.get(service)
    if ct is None:
        raise ValueError(f"{service!r} is not a known connection type")
    out = dict(config)
    for field in ct.secret_fields:
        if field in out and out[field] and is_encrypted(str(out[field])):
            out[field] = decrypt(str(out[field]))
    return out
