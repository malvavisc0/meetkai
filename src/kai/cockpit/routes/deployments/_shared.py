"""Shared constants and helpers for the ``deployments`` route package.

Split out of the former monolithic ``routes/deployments.py`` so each
sub-module (wizard, detail, settings, chats, history, lifecycle) can import
just what it needs without everything living in one file.
"""

from datetime import UTC, datetime

from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from kai.cockpit.bots import (
    CONNECTION_LABELS,
    BotType,
)
from kai.cockpit.connections.service import ConnectionsService
from kai.cockpit.deployments import DeploymentsService, is_connected
from kai.cockpit.models import Deployment, User

# Per-bot-type settings templates — each bot type renders
# its own; the "default" fallback covers unregistered types.
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
    shape — one format everywhere.
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

    Messages are stored as UTC-aware ISO strings; convert to the server's local tz for display.
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
    """Display labels for the ``bt.required_connections`` this operator has not connected yet.

    Uses the same readiness predicate as ``DeploymentsService.start``, so the
    UI start gate can never disagree with what ``start()`` would accept.
    Empty list means the bot type can be started right now.
    """
    conns = {c.service: c for c in ConnectionsService(db).list_for_user(user)}
    missing = [s for s in bt.required_connections if not is_connected(s, conns.get(s))]
    return [CONNECTION_LABELS.get(s, s) for s in missing]
