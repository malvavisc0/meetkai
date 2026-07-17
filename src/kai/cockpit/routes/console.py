"""Console route: /console (list deployments + bot picker) + / placeholder."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from kai.cockpit.app import templates
from kai.cockpit.auth import get_current_user, require_user
from kai.cockpit.bots import BOT_TYPES
from kai.cockpit.connections.service import ConnectionsService
from kai.cockpit.db import get_db
from kai.cockpit.deployments import DeploymentsService, attention_reason
from kai.cockpit.models import User

router = APIRouter()


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

    # Fetch each running deployment's live status once and reuse it for both
    # the attention check and the card's task count, rather than probing the
    # bot process twice per page load.
    status_map = {d.id: svc.fetch_status(d) for d in deployments if d.status == "running"}
    # Interaction counts come from the on-disk history file (no network call),
    # so they're cheap to compute for every card, running or not.
    interaction_summaries = {d.id: svc.interaction_summary(d) for d in deployments}

    attention_reasons = {
        d.id: reason
        for d in deployments
        if (reason := attention_reason(d, status_map.get(d.id), whatsapp_connected)) is not None
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
    return templates.TemplateResponse(request, "index.html", {"user": user})
