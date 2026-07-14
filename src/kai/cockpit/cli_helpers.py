"""Shared helpers used by both the CLI and the web auth routes."""

from kai.cockpit.settings import get_cockpit_settings


def public_url() -> str:
    """The install's public base URL (``KAI_PUBLIC_URL``), trailing slash stripped.

    Empty string when unset — callers decide on a fallback. Magic-link
    emails, the Resend webhook URL shown to operators, and any other
    externally-facing address are built from this so a self-hosted install
    doesn't leak the demo domain.
    """
    return get_cockpit_settings().public_url.rstrip("/")


def build_magic_link_url(token: str) -> str:
    """Build the magic-link URL for a minted token."""
    return f"{public_url()}/login/auth?token={token}"
