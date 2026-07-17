"""Deploy wizard routes: ``GET``/``POST /deployments/new``."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
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
from kai.templates import TemplateRegistry
from kai.templates.resolver import tool_configured_map, validate_tools

router = APIRouter()


def _templates_for(bot_type: str) -> list:
    """Bundled templates for a transport, sorted by display name."""
    templates_list = TemplateRegistry.bundled().list(transport=bot_type)
    templates_list.sort(key=lambda t: t.display_name.lower())
    return templates_list


def _wizard_context(
    request: Request,
    bot_type: str,
    goal: str,
    language: str,
    voice: str,
    template: str,
    db: Session,
    user: User,
    error: str | None = None,
):
    """Re-render the wizard form with the given values and an optional error."""
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
            "templates": _templates_for(bot_type),
            "template": template,
            "missing_connections": missing_required_connections(db, user, bt) if bt else [],
            "error": error,
        },
    )


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
    return _wizard_context(
        request,
        bot_type=bot_type,
        goal=bt.default_goal,
        language=user.language,
        voice=voice,
        template="general",
        db=db,
        user=user,
    )


@router.post("/deployments/new")
async def deploy_new_post(
    request: Request,
    bot_type: str = Form("waha"),
    goal: str = Form(...),
    language: str = Form(...),
    voice: str = Form(""),
    template: str = Form("general"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)

    # Defense-in-depth: reject a hand-crafted POST with a template for the
    # wrong transport (the registry already filters by transport on GET).
    try:
        TemplateRegistry.bundled().get(bot_type, template)
    except FileNotFoundError:
        bt = BOT_TYPES.get(bot_type)
        return _wizard_context(
            request,
            bot_type=bot_type,
            goal=goal,
            language=language,
            voice=voice,
            template=template,
            db=db,
            user=user,
            error=f"Invalid template for {bt.name if bt else bot_type}.",
        )

    try:
        dep = svc.create(user, bot_type, goal, language, voice or None, template=template)
    except (ValueError, ConnectionRequiredError) as exc:
        return _wizard_context(
            request,
            bot_type=bot_type,
            goal=goal,
            language=language,
            voice=voice,
            template=template,
            db=db,
            user=user,
            error=str(exc),
        )

    # Deployment created — go straight to its detail page. The detail page
    # already gates the start button on WhatsApp being connected (showing a
    # "connect WhatsApp" action instead), so the intermediate "ready" step
    # is redundant.
    request.session["flash"] = "Deployment created."
    return RedirectResponse(f"/deployments/{dep.id}", status_code=302)


@router.get("/deployments/new/preview")
async def template_preview(
    request: Request,
    bot_type: str,
    template: str,
    user: User = Depends(require_user),
):
    """AJAX endpoint: render a template preview card for the wizard picker."""
    try:
        tmpl = TemplateRegistry.bundled().get(bot_type, template)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Template not found: {bot_type}/{template}")
    configured = tool_configured_map(tmpl)
    warnings = validate_tools(tmpl)
    return templates.TemplateResponse(
        request,
        "wizard/_template_preview.html",
        {
            "template": tmpl,
            "configured": configured,
            "warnings": warnings,
        },
    )
