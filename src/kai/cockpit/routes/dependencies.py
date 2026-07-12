"""Dependencies route: /dependencies — live service-health probes."""

from fastapi import APIRouter, Depends, Request

from kai.cockpit.app import templates
from kai.cockpit.auth import require_user
from kai.cockpit.models import User
from kai.cockpit.service_health import check_service_health

router = APIRouter()


@router.get("/dependencies")
async def dependencies_page(
    request: Request,
    user: User = Depends(require_user),
):
    service_health = await check_service_health()

    return templates.TemplateResponse(
        request,
        "dependencies.html",
        {
            "user": user,
            "service_health": service_health,
        },
    )
