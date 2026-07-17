"""Shared constants and helpers for the ``deployments`` route package.

Split out of the former monolithic ``routes/deployments.py`` so each
sub-module (wizard, detail, settings, chats, history, lifecycle) can import
just what it needs without everything living in one file.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from kai.cockpit.bots import (
    CONNECTION_LABELS,
    BotType,
)
from kai.cockpit.connections.service import ConnectionsService
from kai.cockpit.deployments import DeploymentsService
from kai.cockpit.models import Deployment, User

# Per-bot-type settings templates. Each bot type renders its own template so
# waha-specific sections (voice, triggers, chats, capabilities, participation)
# never appear on the email bot and vice versa. The "default" fallback covers
# any future bot type that hasn't yet gotten its own template.
SETTINGS_TEMPLATES: dict[str, str] = {
    "waha": "settings_waha.html",
    "email": "settings_email.html",
    "default": "settings_waha.html",
}

# Per-bot-type deploy-wizard templates — same rationale as above.
WIZARD_TEMPLATES: dict[str, str] = {
    "waha": "deploy_wizard_waha.html",
    "email": "deploy_wizard_email.html",
    "default": "deploy_wizard_waha.html",
}

# Services that carry an instruction textarea alongside the toggle.
TOOLS_WITH_INSTRUCTION = frozenset({"database", "smtp", "calcom"})

_HOME_REDIRECT = RedirectResponse("/console", status_code=302)


def build_tools_update(supported_svcs: list[str], form_fields: dict) -> dict[str, dict]:
    """Build the ``settings["tools"]`` dict from the submitted form.

    Every service stores the same ``{"enabled": bool, "instruction": str}``
    shape, whether or not its template renders an instruction textarea
    (``TOOLS_WITH_INSTRUCTION`` gates the textarea, not the storage shape) —
    one format everywhere, no separate flat-bool form to also support.
    """
    tools: dict[str, dict] = {}
    for svc in supported_svcs:
        enabled = f"tool_{svc}" in form_fields
        instruction = form_fields.get(f"tool_{svc}_instruction", "")
        tools[svc] = {"enabled": enabled, "instruction": instruction.strip()}
    return tools


def get_deployment(
    svc: DeploymentsService, dep_id: int, user: User
) -> tuple[DeploymentsService, Deployment] | RedirectResponse:
    """Fetch a deployment and verify ownership; return redirect on failure."""
    dep = svc.get(dep_id)
    if not dep or dep.user_id != user.id:
        return _HOME_REDIRECT
    return svc, dep


def uptime_str(seconds: int) -> str:
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def fmt_ts(ts: str | None) -> str:
    """Render an ISO-8601 timestamp for display in the server's local timezone.

    Messages are stored as UTC-aware ISO strings (see
    ``KaiAgent._now_ts()``), which is the right way to persist them — but
    displaying that raw UTC value labeled "UTC" is misleading for a
    human reading the cockpit from the server's timezone (``TZ`` env var,
    e.g. ``Europe/Berlin``). Convert to the server's local tz for display.
    """
    if not ts:
        return ""
    try:
        parsed = datetime.fromisoformat(ts)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        local = parsed.astimezone()
        tz_label = local.strftime("%Z") or "local"
        return local.strftime(f"%Y-%m-%d %H:%M:%S {tz_label}")
    except ValueError:
        return ts


def missing_required_connections(db: Session, user: User, bt: BotType) -> list[str]:
    """Display labels for the ``bt.required_connections`` this operator has
    not connected yet.

    Empty list means the bot type can be created right now. Shared by the
    wizard's GET (to gate the submit button) and POST (server-side, so a
    disabled button in the DOM is never the only thing standing between an
    operator and a deployment its ``required_connections`` don't satisfy —
    ``DeploymentsService.create()`` enforces the same rule either way).
    """
    if not bt.required_connections:
        return []
    connected = {
        c.service for c in ConnectionsService(db).list_for_user(user) if c.status == "connected"
    }
    missing = [service for service in bt.required_connections if service not in connected]
    return [CONNECTION_LABELS.get(service, service) for service in missing]
