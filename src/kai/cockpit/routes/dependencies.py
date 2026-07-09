"""Dependencies route: /dependencies — live service-health probes."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from kai.cockpit.app import templates
from kai.cockpit.auth import require_user
from kai.cockpit.connections import ConnectionsService
from kai.cockpit.db import get_db
from kai.cockpit.models import User
from kai.cockpit.service_health import check_service_health

router = APIRouter()


@router.get("/dependencies")
async def dependencies_page(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    conn_svc = ConnectionsService(db)
    whatsapp = conn_svc.get_whatsapp(user)

    service_health = await check_service_health(
        whatsapp_status=(whatsapp.status if whatsapp else None),
    )

    return templates.TemplateResponse(
        request,
        "dependencies.html",
        {
            "user": user,
            "service_health": service_health,
        },
    )
