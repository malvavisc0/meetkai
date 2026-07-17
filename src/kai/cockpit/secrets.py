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

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic_settings import BaseSettings, SettingsConfigDict

_HKDF_INFO = b"kai-connection-secrets"
_VERSION_SEP = ":"
_key_cache: dict[str, Fernet] = {}

# Active key version for new encrypt() calls. Set by key rotation so the
# running process encrypts new secrets under the new version without
# mutating the process environment; the rotation command persists it to
# ``.env`` for the next process start. ``None`` means read from
# ``KAI_CREDENTIAL_KEY_VERSION`` on each call.
_active_version_override: str | None = None


class EncryptionSettings(BaseSettings):
    """Root secret + active key version for credential encryption."""

    model_config = SettingsConfigDict(env_prefix="KAI_", env_file=".env", extra="ignore")

    credential_encryption_key: str = ""
    credential_key_version: str = ""


def get_encryption_settings() -> EncryptionSettings:
    return EncryptionSettings()


def _root_key_material() -> bytes:
    settings = get_encryption_settings()
    if not settings.credential_encryption_key:
        raise RuntimeError(
            "KAI_CREDENTIAL_ENCRYPTION_KEY is not set — required for credential "
            "encryption. Generate with: openssl rand -hex 32"
        )
    return settings.credential_encryption_key.encode()


def _active_version() -> str:
    if _active_version_override is not None:
        return _active_version_override
    settings = get_encryption_settings()
    if not settings.credential_key_version:
        raise RuntimeError(
            "KAI_CREDENTIAL_KEY_VERSION is not set — required for credential "
            "encryption. Set it to the active key version (e.g. v1) in .env."
        )
    return settings.credential_key_version


def set_active_version(version: str) -> None:
    """Set the active encryption key version for subsequent ``encrypt()`` calls.

    Used by key rotation: after re-encrypting every credential under a new
    version, new credentials must encrypt under that same version. Held here
    as explicit module state (and persisted to ``.env`` by the rotation
    command) rather than mutating ``os.environ``.
    """
    global _active_version_override
    _active_version_override = version


def _derive_fernet(version: str) -> Fernet:
    """Derive (and cache) the Fernet key for a given version.

    One cache entry per version ever used, so tiny. Avoids re-running HKDF
    on every decrypt.
    """
    if version in _key_cache:
        return _key_cache[version]
    info = _HKDF_INFO + b":" + version.encode()
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=info,
    )
    raw = hkdf.derive(_root_key_material())
    fernet = Fernet(base64.urlsafe_b64encode(raw))
    _key_cache[version] = fernet
    return fernet


def _clear_key_cache() -> None:
    """Drop cached Fernet instances and reset the active-version override.

    Test/rotation use only: clears derived Fernet keys (keyed by version)
    and the active-version override so a fresh key/version is picked up
    from env on the next call.
    """
    global _active_version_override
    _key_cache.clear()
    _active_version_override = None


def encrypt(plaintext: str) -> str:
    """Encrypt a secret, returning a versioned ciphertext envelope."""
    version = _active_version()
    fernet = _derive_fernet(version)
    token = fernet.encrypt(plaintext.encode()).decode()
    return f"{version}{_VERSION_SEP}{token}"


def decrypt(ciphertext: str) -> str:
    """Decrypt a versioned ciphertext envelope. Raises ValueError on failure."""
    if _VERSION_SEP not in ciphertext:
        raise ValueError("ciphertext missing version envelope")
    version, token = ciphertext.split(_VERSION_SEP, 1)
    fernet = _derive_fernet(version)
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
    if _VERSION_SEP not in value:
        return False
    version, _ = value.split(_VERSION_SEP, 1)
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
