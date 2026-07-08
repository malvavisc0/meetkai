"""Shared kai-vNNN-<slug> naming scheme for external service identifiers.

Both WAHA sessions (``ConnectionsService`` via :func:`kai.utils.common.user_slug`)
and LightRAG workspaces (``BrainsService`` via the same helper) need a stable,
deterministic, external-service-legal identifier derived from a user's
email, version-pinned so a future breaking change can mint a fresh
session/workspace without colliding with (or silently reusing) the old one.

    bob@test.com      -> kai-v001-bob_at_test_com
    alice@example.org -> kai-v001-alice_at_example_org
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version

_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_-]+")
_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]+")

_FALLBACK_VERSION = "000"


def kai_version_slug() -> str:
    """Return the naming-scheme version segment derived from kai's own package version.

    e.g. installed version ``0.0.1`` -> ``001`` -> ``kai-v001-...``. Dots and
    any other non-alphanumeric characters are stripped since WAHA/LightRAG
    identifiers only allow ``[a-zA-Z0-9_-]``. Falls back to
    :data:`_FALLBACK_VERSION` if kai isn't installed as a package (e.g. some
    test/dev setups), so naming never raises.
    """
    try:
        raw = version("kai")
    except PackageNotFoundError:
        return _FALLBACK_VERSION
    slug = _NON_ALNUM.sub("", raw)
    return slug or _FALLBACK_VERSION


def kai_slug_for(email: str, *, version: str | None = None) -> str:
    """Return a ``kai-v<version>-<slug>`` identifier derived from ``email``.

    ``@`` becomes ``_at_`` and ``.`` becomes ``_``; any other illegal
    character run collapses into a single ``-``. Legal in both WAHA session
    names and LightRAG workspace names ([a-zA-Z0-9_-]).

    ``version`` defaults to :func:`kai_version_slug` (kai's own installed
    package version, e.g. ``0.0.1`` -> ``001``) so the naming scheme tracks
    kai's actual version automatically instead of a hand-maintained
    constant. Pass an explicit value only to pin/rotate independently of
    the package version.
    """
    if version is None:
        version = kai_version_slug()
    stem = email.replace("@", "_at_").replace(".", "_")
    stem = _INVALID_CHARS.sub("-", stem).strip("-")
    return f"kai-v{version}-{stem}"
