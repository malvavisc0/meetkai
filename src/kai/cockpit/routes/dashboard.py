"""Dashboard route: / (list deployments + bot picker)."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from kai.cockpit.app import templates
from kai.cockpit.auth import require_user
from kai.cockpit.bots import BOT_TYPES
from kai.cockpit.db import get_db
from kai.cockpit.deployments import DeploymentsService
from kai.cockpit.models import User

router = APIRouter()


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

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "deployments": deployments,
            "available_types": available_types,
            "flash": flash,
        },
    )
