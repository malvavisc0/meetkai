"""``kai-vNNN-<slug>`` naming scheme for external service identifiers."""

import re
from importlib.metadata import PackageNotFoundError, version

_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_-]+")
_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]+")

_FALLBACK_VERSION = "000"


def kai_version_slug() -> str:
    """Naming-scheme version segment from kai's installed package version.

    e.g. ``0.0.1`` -> ``001`` -> ``kai-v001-...``. Falls back to
    ``_FALLBACK_VERSION`` if kai isn't installed as a package.
    """
    try:
        raw = version("kai")
    except PackageNotFoundError:
        return _FALLBACK_VERSION
    slug = _NON_ALNUM.sub("", raw)
    return slug or _FALLBACK_VERSION


def kai_slug_for(email: str, *, version: str | None = None) -> str:
    """Return a ``kai-v<version>-<slug>`` identifier derived from ``email``.

    ``@`` becomes ``_at_``, ``.`` becomes ``_``, other illegal runs collapse
    to a single ``-``. Result is legal in both WAHA and LightRAG identifiers.
    ``version`` defaults to :func:`kai_version_slug` (kai's installed package
    version).
    """
    if version is None:
        version = kai_version_slug()
    stem = email.replace("@", "_at_").replace(".", "_")
    stem = _INVALID_CHARS.sub("-", stem).strip("-")
    return f"kai-v{version}-{stem}"
