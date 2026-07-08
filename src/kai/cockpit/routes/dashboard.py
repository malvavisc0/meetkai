"""Dashboard route: / (list deployments + bot picker)."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from kai.cockpit.app import templates
from kai.cockpit.auth import require_user
from kai.cockpit.bots import BOT_TYPES
from kai.cockpit.connections import ConnectionsService
from kai.cockpit.db import get_db
from kai.cockpit.deployments import DeploymentsService
from kai.cockpit.models import Deployment, User

router = APIRouter()


def _attention_reason(
    svc: DeploymentsService, dep: Deployment, whatsapp_connected: bool
) -> str | None:
    """Why a deployment needs the operator's action *now*, or None if it doesn't.

    - WhatsApp disconnected while the bot is meant to be running (intent unmet).
    - A ``running`` row whose live status probe comes back empty (process died
      but the row wasn't reconciled — reconciliation only runs at startup).
    - A running deployment with unapplied settings changes (needs_restart).

    A failed start decays to the neutral ``stopped`` state on the next load
    rather than staying red — red is reserved for states needing action now.

    The route computes this once per deployment and hands the same reasons to
    both the health summary count and the per-row badge, so the two can never
    disagree about which deployments need attention.
    """
    if dep.desired_state == "running" and not whatsapp_connected:
        return "WhatsApp down, wants running"
    if dep.status == "running":
        if svc.fetch_status(dep) is None:
            return "Bot process isn't responding"
        if dep.needs_restart:
            return "Restart needed to apply settings"
    return None


@router.get("/")
async def dashboard(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    deployments = svc.list_for_user(user.id)
    deployed_types = {d.bot_type for d in deployments}
    available_types = [BOT_TYPES[bt] for bt in BOT_TYPES if bt not in deployed_types]

    conn_svc = ConnectionsService(db)
    whatsapp = conn_svc.get_whatsapp(user)
    whatsapp_connected = bool(whatsapp and whatsapp.status == "connected")

    running = sum(1 for d in deployments if d.status == "running")
    stopped = sum(1 for d in deployments if d.status == "stopped")
    attention_reasons = {
        d.id: reason
        for d in deployments
        if (reason := _attention_reason(svc, d, whatsapp_connected)) is not None
    }

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "deployments": deployments,
            "available_types": available_types,
            "whatsapp_connected": whatsapp_connected,
            "running": running,
            "stopped": stopped,
            "attention_reasons": attention_reasons,
            "flash": flash,
        },
    )
