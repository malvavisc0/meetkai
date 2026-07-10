"""Shared helpers used by both the CLI and the web auth routes."""

import os


def build_magic_link_url(token: str) -> str:
    """Build the magic-link URL for a minted token."""
    public_url = os.environ.get("KAI_COCKPIT_PUBLIC_URL", "").rstrip("/")
    return f"{public_url}/login/auth?token={token}"
