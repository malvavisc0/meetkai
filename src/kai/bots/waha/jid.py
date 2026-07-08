"""Shared JID and display-name helpers for the WAHA bot."""

from __future__ import annotations


def user_digits(jid: str) -> str:
    """Extract the bare digit prefix from a WhatsApp JID (before ``@``)."""
    return jid.split("@")[0]


def sanitize_display_name(name: str) -> str:
    """Sanitize a user-controlled display name for safe interpolation.

    Strips brackets (``[]``), replaces newlines with spaces, and caps at 80
    characters so a malicious pushname can't break out of ``@[...]`` wrappers
    or inject arbitrary text into prompt context.
    """
    return name.replace("[", "").replace("]", "").replace("\n", " ").strip()[:80]
