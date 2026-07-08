"""Chat routes: /deployments/{id}/chat."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from kai.cockpit.app import templates
from kai.cockpit.auth import require_user
from kai.cockpit.db import get_db
from kai.cockpit.deployments import DeploymentsService
from kai.cockpit.models import Deployment, User

router = APIRouter()

_HOME_REDIRECT = RedirectResponse("/", status_code=302)


def _get_deployment(
    svc: DeploymentsService, dep_id: int, user: User
) -> tuple[DeploymentsService, Deployment] | RedirectResponse:
    dep = svc.get(dep_id)
    if not dep or dep.user_id != user.id:
        return _HOME_REDIRECT
    return svc, dep


@router.get("/deployments/{dep_id}/chat")
async def chat_page(
    request: Request,
    dep_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = _get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result

    reply = request.session.pop("chat_reply", None)
    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "user": user,
            "dep": dep,
            "dep_user": user,
            "reply": reply,
        },
    )


@router.post("/deployments/{dep_id}/chat")
async def chat_send(
    request: Request,
    dep_id: int,
    message: str = Form(...),
    persist: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = _get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result

    result_dict = svc.send_message(dep, message, persist=persist == "true")
    reply = result_dict.get("reply", "(no reply)")
    request.session["chat_reply"] = reply
    return RedirectResponse(f"/deployments/{dep_id}/chat", status_code=302)


@router.post("/deployments/{dep_id}/chat/clear")
async def chat_clear(
    dep_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = _get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result

    svc.clear_history(dep)
    return RedirectResponse(f"/deployments/{dep_id}/chat", status_code=302)
