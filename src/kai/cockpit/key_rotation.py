"""On-demand credential encryption key rotation.

Rotation is operator-initiated (run ``kai cockpit rotate-credential-key``),
not tracked or scheduled by the application. The cockpit has a small number
of credential-bearing Connection rows; building automated rotation-compliance
tracking for that would be disproportionate machinery with no concrete
requirement behind it.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def rotate_credential_key(db: Session) -> tuple[str, bool]:
    """Re-encrypt every credential Connection under a new key version.

    Returns ``(new_version, env_written)``. ``env_written`` is ``False`` when
    ``.env`` doesn't exist (Docker, env-in-compose) so the CLI can print a
    manual instruction. ``KAI_CREDENTIAL_ENCRYPTION_KEY`` (the root secret) is
    never touched — only the version is bumped.
    """
    from kai.cockpit.bots import CREDENTIAL_TYPES
    from kai.cockpit.models import Connection
    from kai.cockpit.secrets import (
        _clear_key_cache,
        decrypt_config,
        encrypt_config,
        get_encryption_settings,
    )

    current_version = get_encryption_settings().credential_key_version
    if not current_version:
        raise RuntimeError(
            "KAI_CREDENTIAL_KEY_VERSION is not set — cannot rotate. "
            "Set it to the current version (e.g. v1) in .env first."
        )

    num = int(current_version.lstrip("v"))
    new_version = f"v{num + 1}"

    os.environ["KAI_CREDENTIAL_KEY_VERSION"] = new_version
    _clear_key_cache()

    reencrypted = 0
    for conn in (
        db.query(Connection)
        .filter(Connection.service.in_(list(CREDENTIAL_TYPES)))
        .all()
    ):
        plaintext_config = decrypt_config(conn.service, conn.config)
        conn.config = encrypt_config(conn.service, plaintext_config)
        reencrypted += 1

    db.commit()
    env_written = _update_env_file(new_version)

    logger.info(
        "Credential key rotated: %s -> %s (%d connections re-encrypted)",
        current_version, new_version, reencrypted,
    )
    return new_version, env_written


def _update_env_file(version: str) -> bool:
    """Update ``KAI_CREDENTIAL_KEY_VERSION`` in ``.env``.

    Returns ``True`` if the file was written. Returns ``False`` if ``.env``
    doesn't exist (Docker, env-in-compose) so the caller can print the manual
    instruction.
    """
    env_path = Path(".env")
    if not env_path.is_file():
        return False

    text = env_path.read_text(encoding="utf-8")
    pattern = re.compile(r"^KAI_CREDENTIAL_KEY_VERSION\s*=.*$", re.MULTILINE)
    replacement = f"KAI_CREDENTIAL_KEY_VERSION={version}"

    if pattern.search(text):
        text = pattern.sub(replacement, text)
    else:
        text = text.rstrip("\n") + f"\n{replacement}\n"

    env_path.write_text(text, encoding="utf-8")
    return True
