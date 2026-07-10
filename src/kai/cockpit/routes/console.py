"""Console route: /console (list deployments + bot picker) + / placeholder."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from kai.cockpit.app import templates
from kai.cockpit.auth import get_current_user, require_user
from kai.cockpit.bots import BOT_TYPES
from kai.cockpit.brains import BrainsService
from kai.cockpit.connections import ConnectionsService
from kai.cockpit.db import get_db
from kai.cockpit.deployments import DeploymentsService
from kai.cockpit.models import Deployment, User

router = APIRouter()


def _attention_reason(
    dep: Deployment, status_data: dict | None, whatsapp_connected: bool
) -> str | None:
    """Why a deployment needs the operator's action *now*, or None if it doesn't.

    - WhatsApp disconnected while the bot is meant to be running (intent unmet).
    - A ``running`` row whose live status probe comes back empty (process died
      but the row wasn't reconciled — reconciliation only runs at startup).
    - A running deployment with unapplied settings changes (needs_restart).

    A failed start decays to the neutral ``stopped`` state on the next load
    rather than staying red — red is reserved for states needing action now.

    ``status_data`` is the live ``/status`` probe result for ``dep`` (``None``
    if it isn't running or the probe failed) — the caller fetches it once per
    running deployment and reuses it here and for the card's task count, so
    the route never doubles the number of live status calls.

    The route computes this once per deployment and hands the same reasons to
    both the health summary count and the per-row badge, so the two can never
    disagree about which deployments need attention.
    """
    if dep.desired_state == "running" and not whatsapp_connected:
        return "WhatsApp down, wants running"
    if dep.status == "running":
        if status_data is None:
            return "Bot process isn't responding"
        if dep.needs_restart:
            return "Restart needed to apply settings"
    return None


@router.get("/console")
async def console(
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
    brain_initialized = BrainsService(db).get_brain(user) is not None

    # Fetch each running deployment's live status once and reuse it for both
    # the attention check and the card's task count, rather than probing the
    # bot process twice per page load.
    status_map = {d.id: svc.fetch_status(d) for d in deployments if d.status == "running"}
    # Interaction counts come from the on-disk history file (no network call),
    # so they're cheap to compute for every card, running or not.
    interaction_summaries = {d.id: svc.interaction_summary(d) for d in deployments}

    running = sum(1 for d in deployments if d.status == "running")
    stopped = sum(1 for d in deployments if d.status == "stopped")
    attention_reasons = {
        d.id: reason
        for d in deployments
        if (reason := _attention_reason(d, status_map.get(d.id), whatsapp_connected)) is not None
    }

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(
        request,
        "console.html",
        {
            "user": user,
            "deployments": deployments,
            "available_types": available_types,
            "bot_types": BOT_TYPES,
            "whatsapp_connected": whatsapp_connected,
            "brain_initialized": brain_initialized,
            "running": running,
            "stopped": stopped,
            "attention_reasons": attention_reasons,
            "status_map": status_map,
            "interaction_summaries": interaction_summaries,
            "flash": flash,
        },
    )


@router.get("/")
async def index(
    request: Request,
    user: User | None = Depends(get_current_user),
):
    """Placeholder landing page.

    Authenticated users are redirected to /console; anonymous users see a
    simple landing page with a link to log in.
    """
    if user:
        return RedirectResponse("/console", status_code=302)
    return templates.TemplateResponse(request, "index.html", {"user": None})
