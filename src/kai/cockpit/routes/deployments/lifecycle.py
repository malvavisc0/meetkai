"""Deployment lifecycle actions: start, stop, sleep, wake, restart, delete."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from kai.cockpit.auth import require_user
from kai.cockpit.db import get_db
from kai.cockpit.deployments import (
    ConnectionRequiredError,
    DeploymentsService,
    DeploymentStartupError,
)
from kai.cockpit.models import User
from kai.cockpit.routes.deployments._shared import get_deployment

router = APIRouter()


@router.post("/deployments/{dep_id}/start")
async def deployment_start(
    request: Request,
    dep_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result
    try:
        svc.start(dep)
    except ConnectionRequiredError:
        request.session["flash"] = "Connect WhatsApp first before starting."
        return RedirectResponse("/connections", status_code=302)
    except DeploymentStartupError as exc:
        request.session["flash"] = f"Could not start deployment: {exc}"
    return RedirectResponse(f"/deployments/{dep_id}", status_code=302)


@router.post("/deployments/{dep_id}/stop")
async def deployment_stop(
    dep_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result
    svc.stop(dep)
    return RedirectResponse(f"/deployments/{dep_id}", status_code=302)


@router.post("/deployments/{dep_id}/sleep")
async def deployment_sleep(
    dep_id: int,
    chat_id: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result
    svc.sleep_chat(dep, chat_id)
    return RedirectResponse(f"/deployments/{dep_id}", status_code=302)


@router.post("/deployments/{dep_id}/wake")
async def deployment_wake(
    dep_id: int,
    chat_id: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result
    svc.wake_chat(dep, chat_id)
    return RedirectResponse(f"/deployments/{dep_id}", status_code=302)


@router.post("/deployments/{dep_id}/restart")
async def deployment_restart(
    request: Request,
    dep_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result
    try:
        svc.stop(dep)
        svc.start(dep)
    except (ConnectionRequiredError, DeploymentStartupError) as exc:
        request.session["flash"] = f"restart failed: {exc}"
    except Exception as exc:
        # stop() can raise (e.g. ProcessLookupError from a recycled PID);
        # surface it rather than letting it propagate as an unhandled 500.
        request.session["flash"] = f"restart failed: {exc}"
    return RedirectResponse(f"/deployments/{dep_id}", status_code=302)


@router.post("/deployments/{dep_id}/delete")
async def deployment_delete(
    request: Request,
    dep_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a deployment. WhatsApp connection is left intact."""
    svc = DeploymentsService(db)
    result = get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result
    svc.delete(dep)
    request.session["flash"] = "Deployment deleted."
    return RedirectResponse("/console", status_code=302)
