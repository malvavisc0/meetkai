"""Deploy wizard routes: ``GET``/``POST /deployments/new``."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from kai.cockpit.app import templates
from kai.cockpit.auth import require_user
from kai.cockpit.bots import BOT_TYPES, auto_pick_voice
from kai.cockpit.db import get_db
from kai.cockpit.deployments import ConnectionRequiredError, DeploymentsService
from kai.cockpit.models import User
from kai.cockpit.routes.deployments._shared import (
    ALL_LANGUAGES,
    ALL_VOICES,
    WIZARD_TEMPLATES,
    missing_required_connections,
)

router = APIRouter()


@router.get("/deployments/new")
async def deploy_new_get(
    request: Request,
    bot_type: str = "waha",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    bt = BOT_TYPES.get(bot_type)
    if bt is None:
        return RedirectResponse("/console", status_code=302)

    dep_svc = DeploymentsService(db)
    existing = dep_svc.get_for_user_and_type(user.id, bot_type)
    if existing:
        return RedirectResponse(f"/deployments/{existing.id}", status_code=302)

    voice = auto_pick_voice(user.language)
    wizard_template = WIZARD_TEMPLATES.get(bot_type, WIZARD_TEMPLATES["default"])
    return templates.TemplateResponse(
        request,
        wizard_template,
        {
            "user": user,
            "step": "config",
            "bot_type": bot_type,
            "bt": bt,
            "goal": bt.default_goal,
            "language": user.language,
            "voice": voice,
            "voices": ALL_VOICES,
            "languages": ALL_LANGUAGES,
            "missing_connections": missing_required_connections(db, user, bt),
        },
    )


@router.post("/deployments/new")
async def deploy_new_post(
    request: Request,
    bot_type: str = Form("waha"),
    goal: str = Form(...),
    language: str = Form(...),
    voice: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    try:
        dep = svc.create(user, bot_type, goal, language, voice or None)
    except (ValueError, ConnectionRequiredError) as exc:
        bt = BOT_TYPES.get(bot_type)
        wizard_template = WIZARD_TEMPLATES.get(bot_type, WIZARD_TEMPLATES["default"])
        return templates.TemplateResponse(
            request,
            wizard_template,
            {
                "user": user,
                "step": "config",
                "bot_type": bot_type,
                "bt": bt,
                "goal": goal,
                "language": language,
                "voice": voice,
                "voices": ALL_VOICES,
                "languages": ALL_LANGUAGES,
                "missing_connections": missing_required_connections(db, user, bt) if bt else [],
                "error": str(exc),
            },
        )

    # Deployment created — go straight to its detail page. The detail page
    # already gates the start button on WhatsApp being connected (showing a
    # "connect WhatsApp" action instead), so the intermediate "ready" step
    # is redundant.
    request.session["flash"] = "Deployment created."
    return RedirectResponse(f"/deployments/{dep.id}", status_code=302)
