"""Small shared helpers used across cockpit, runs, and CLI modules.

Kept dependency-free (no SQLAlchemy / FastAPI imports) so any module can
import these without pulling in the web stack.
"""

import hashlib
import hmac as hmac_mod
from datetime import UTC, datetime

_HMAC_ALGORITHMS = {"sha256": hashlib.sha256, "sha512": hashlib.sha512}


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string with timezone info."""
    return datetime.now(UTC).isoformat()


def user_slug(kai_slug: str | None) -> str:
    """Normalize a user's ``kai_slug`` (None -> "") for WAHA/Brain naming.

    Accepts the raw slug value rather than a ``User`` object so this module
    stays free of SQLAlchemy imports; callers pass ``user.kai_slug``.
    """
    return kai_slug or ""


def compute_hmac(key: str, body: bytes, algorithm: str = "sha512") -> str:
    """Compute an HMAC signature for webhook authentication.

    Shared by ``cockpit/deployments.py`` and ``cli/bot.py`` so the signing
    logic isn't duplicated.
    """
    algo = _HMAC_ALGORITHMS.get(algorithm.lower(), hashlib.sha512)
    return hmac_mod.new(key.encode(), body, algo).hexdigest()
