"""Chat send/clear actions for a deployment.

The chat UI itself lives on the deployment detail page
(``GET /deployments/{id}``, see ``routes/deployments.py``) — this module only
handles the POST actions it submits to.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from kai.cockpit.auth import require_user
from kai.cockpit.db import get_db
from kai.cockpit.deployments import DeploymentsService
from kai.cockpit.models import Deployment, User

router = APIRouter()

_HOME_REDIRECT = RedirectResponse("/console", status_code=302)


def _get_deployment(
    svc: DeploymentsService, dep_id: int, user: User
) -> tuple[DeploymentsService, Deployment] | RedirectResponse:
    dep = svc.get(dep_id)
    if not dep or dep.user_id != user.id:
        return _HOME_REDIRECT
    return svc, dep


@router.post("/deployments/{dep_id}/chat")
async def chat_send(
    request: Request,
    dep_id: int,
    message: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = _get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result

    # persist=False always: a permanent goal change (the only thing this
    # flag gates, see Bot._operator_tools) should only ever happen through
    # the explicit Goal field on the Settings page, never as a side effect
    # of a casual test message here.
    result_dict = svc.send_message(dep, message, persist=False)
    reply = result_dict.get("reply", "(no reply)")
    request.session["chat_reply"] = reply
    return RedirectResponse(f"/deployments/{dep_id}", status_code=302)


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
    return RedirectResponse(f"/deployments/{dep_id}", status_code=302)
