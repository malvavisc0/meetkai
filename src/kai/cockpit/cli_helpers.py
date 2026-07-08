"""Shared helpers used by both the CLI and the web auth routes."""

import os


def build_magic_link_url(token: str) -> str:
    """Build the magic-link URL for a minted token.

    Prefers KAI_COCKPIT_PUBLIC_URL (a full base like
    ``https://kai.example.com``) so Docker/proxied deployments emit links
    that actually reach the cockpit. Falls back to KAI_COCKPIT_HOST:PORT.
    """
    public_url = os.environ.get("KAI_COCKPIT_PUBLIC_URL", "").rstrip("/")
    if public_url:
        return f"{public_url}/login/auth?token={token}"
    host = os.environ.get("KAI_COCKPIT_HOST", "127.0.0.1")
    port = os.environ.get("KAI_COCKPIT_PORT", "8080")
    return f"http://{host}:{port}/login/auth?token={token}"
