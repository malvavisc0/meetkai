"""Deployment detail page: ``GET /deployments/{dep_id}``."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from kai.cockpit.app import templates
from kai.cockpit.auth import require_user
from kai.cockpit.bots import BOT_TYPES, CAPABILITY_LABELS
from kai.cockpit.connections.service import ConnectionsService
from kai.cockpit.db import get_db
from kai.cockpit.deployments import DeploymentsService, attention_reason
from kai.cockpit.models import User
from kai.cockpit.routes.deployments._shared import (
    get_deployment,
    missing_required_connections,
    uptime_str,
)

router = APIRouter()


@router.get("/deployments/{dep_id}")
async def deployment_detail(
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

    # Hide start button when the bot's required connections aren't all ready
    # — the operator should never see a start that deploy.start() would refuse.
    conn_svc = ConnectionsService(db)
    whatsapp = await conn_svc.refresh_status_if_stale(user)
    whatsapp_connected = bool(whatsapp and whatsapp.status == "connected")

    bt = BOT_TYPES.get(dep.bot_type)
    missing_connections = missing_required_connections(db, user, bt) if bt else []

    status_data = None
    uptime_display = None
    uptime_s = None
    if dep.status == "running":
        status_data = svc.fetch_status(dep)
        started_at = svc.run_started_at(dep)
        if started_at:
            try:
                started = datetime.fromisoformat(started_at)
                if started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                delta = int((datetime.now(UTC) - started).total_seconds())
                uptime_s = max(0, delta)
                uptime_display = uptime_str(uptime_s)
            except (ValueError, TypeError):
                pass

    # Same signal as console — a running bot whose WhatsApp disconnected looks
    # identical to a healthy one otherwise, so a direct page visit wouldn't
    # show messages silently failing.
    reason = attention_reason(dep, status_data, whatsapp_connected)

    flash = request.session.pop("flash", None)
    # needs_restart is persisted (survives reloads/new tabs).
    needs_restart = bool(dep.needs_restart) and dep.status == "running"

    conversation_count, message_count = svc.interaction_summary(dep)
    reply = request.session.pop("chat_reply", None)
    sent_to = request.session.pop("chat_sent_to", None)
    sleep_supported = bool(BOT_TYPES.get(dep.bot_type) and BOT_TYPES[dep.bot_type].supports_sleep)

    return templates.TemplateResponse(
        request,
        "deployment.html",
        {
            "user": user,
            "dep": dep,
            "dep_user": user,
            "status": status_data,
            "uptime_str": uptime_display,
            "uptime_s": uptime_s,
            "needs_restart": needs_restart,
            "missing_connections": missing_connections,
            "attention_reason": reason,
            "conversation_count": conversation_count,
            "message_count": message_count,
            "capability_labels": CAPABILITY_LABELS,
            "sleep_supported": sleep_supported,
            "reply": reply,
            "sent_to": sent_to,
            "flash": flash,
        },
    )
