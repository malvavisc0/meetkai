"""WAHA chat picker proxy for the settings page's whitelist/blacklist UI.

``GET /deployments/{dep_id}/chats.json``. This is WAHA-only (fetches the
operator's live WhatsApp chat list); the email bot has no equivalent and
never renders the picker widget.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from kai.bots.waha.client import WahaClient
from kai.bots.waha.config import get_waha_settings
from kai.cockpit.auth import require_user
from kai.cockpit.connections import ConnectionsService
from kai.cockpit.db import get_db
from kai.cockpit.deployments import DeploymentsService
from kai.cockpit.models import User
from kai.cockpit.routes.deployments._shared import get_deployment

logger = logging.getLogger(__name__)

router = APIRouter()


def _avatar_initial(name: str | None, chat_id: str) -> str:
    """Pick a 1–2 char avatar label for a chat row."""
    label = (name or "").strip()
    if label:
        return label[0].upper()
    return (chat_id or "?")[0].upper()


@router.get("/deployments/{dep_id}/chats.json")
async def deployment_chats_json(
    dep_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Proxy WAHA's chat overview for the chat picker on the Settings page.

    Returns ``{"chats": [{id, name, avatar_initial}], "has_more": bool}``,
    trimmed down from WAHA's ``ChatSummary``. If the user has no WhatsApp
    connection there are genuinely no chats to list, so an empty list with
    a 200 is returned. If the session-scoped ``chats/overview`` call fails
    (session-level WAHA/puppeteer error, timeout, etc.) the response
    carries an ``error`` message instead of an empty chat list — the picker
    JS shows it and links the user to ``/connections`` rather than
    silently rendering nothing. This is deliberately *not* pointed at
    ``/dependencies``: that page only probes WAHA's general ``/health``
    endpoint and can be perfectly green while this operator's specific
    session is failing — see ``service_health.py``.
    """
    svc = DeploymentsService(db)
    result = get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"chats": [], "has_more": False})

    conn = ConnectionsService(db).get_whatsapp(user)
    if not conn or not conn.config.get("waha_session"):
        return JSONResponse({"chats": [], "has_more": False})

    try:
        settings = get_waha_settings().model_copy(update={"session": conn.config["waha_session"]})
        client = WahaClient(settings)
        try:
            # Over-fetch by one so has_more is reliable even when WAHA's
            # merge=true collapses @lid/@c.us pairs below the requested limit.
            raw = await client.get_chats_overview(limit=limit + 1, offset=offset)
        finally:
            await client.close()
    except Exception:
        logger.exception(
            "Chat picker: WAHA chats/overview request failed for dep_id=%s session=%s",
            dep_id,
            conn.config["waha_session"],
        )
        return JSONResponse(
            {
                "chats": [],
                "has_more": False,
                "error": "Could not load chats for this WhatsApp session",
            }
        )

    has_more = len(raw) > limit
    chats = raw[:limit]
    trimmed = [
        {
            "id": c.get("id", ""),
            "name": c.get("name") or "",
            "avatar_initial": _avatar_initial(c.get("name"), c.get("id", "")),
        }
        for c in chats
        if c.get("id")
    ]
    return JSONResponse({"chats": trimmed, "has_more": has_more})
