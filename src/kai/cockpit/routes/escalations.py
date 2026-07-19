"""Cockpit escalation routes: dashboard page, list, ingest, and resolve.

The dashboard and mutating routes require auth (the dashboard renders
``base.html``, which expects the logged-in ``user``, and resolving mutates
state). The JSON list endpoints are read-only and unauthenticated, matching
:mod:`health`'s route pattern — the cockpit is typically behind a reverse
proxy or in a private network.

The ``POST /api/escalations`` ingest endpoint is the webhook bots POST to
(via ``forward_to_cockpit`` from ``BaseBot.on_escalation``) so escalations
fired in a bot subprocess land in the cockpit's own store and show up on the
dashboard. It is unauthenticated for the same network-trust reason as the
read endpoints — the cockpit passes its URL to the bots it spawns, and the
payload is a plain escalation record (no credentials).
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from kai.agent.tools.escalate import (
    Escalation,
    EscalationStore,
    get_active_escalations,
    list_escalations,
    resolve_escalation,
)
from kai.cockpit.app import templates
from kai.cockpit.auth import require_user
from kai.cockpit.db import get_db
from kai.cockpit.models import User
from kai.cockpit.settings import get_cockpit_settings

router = APIRouter()


def _check_escalation_secret(request: Request) -> JSONResponse | None:
    """Validate the bearer token when KAI_COCKPIT_ESCALATION_SECRET is set.

    Returns a 401 JSONResponse on mismatch, or None when auth passes / is off
    (the default — the cockpit is behind a reverse proxy / private network,
    matching the existing read-only /api routes' trust model).
    """
    secret = get_cockpit_settings().cockpit_escalation_secret
    if not secret:
        return None
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.lower().startswith("bearer ") else ""
    if token != secret:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return None


@router.get("/escalations")
async def dashboard(request: Request, user: User = Depends(require_user)):
    """Escalation dashboard: active escalations + resolved history."""
    all_escs = await list_escalations()
    active = [e for e in all_escs if not e.resolved]
    resolved = [e for e in all_escs if e.resolved]
    return templates.TemplateResponse(
        request,
        "escalations.html",
        {"user": user, "active": active, "resolved": resolved},
    )


@router.get("/api/escalations")
async def list_all() -> dict:
    """Return all escalation events (JSON)."""
    escalations = await list_escalations()
    return {"escalations": [e.model_dump(mode="json") for e in escalations]}


@router.get("/api/escalations/active")
async def list_active() -> dict:
    """Return active (unresolved) escalation events (JSON)."""
    active = await get_active_escalations()
    return {
        "escalations": [e.model_dump(mode="json") for e in active],
        "count": len(active),
    }


@router.post("/api/escalations")
async def ingest(request: Request) -> JSONResponse:
    """Receive an escalation posted by a bot's ``forward_to_cockpit``.

    The body is an ``Escalation.to_dict()`` (ISO-string timestamps). The
    cockpit stores it verbatim — the bot's generated id and created_at are
    kept so the cockpit's view and the bot's local store agree on identity.
    Returns 201 + the stored record, or 400 on a malformed payload (a
    non-dict body is rejected by FastAPI as 422 before this runs). When
    ``KAI_COCKPIT_ESCALATION_SECRET`` is set, a matching ``Authorization:
    Bearer <secret>`` header is required (401 otherwise).
    """
    denied = _check_escalation_secret(request)
    if denied is not None:
        return denied
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse(
            {"ok": False, "error": "payload must be a JSON object"}, status_code=400
        )
    try:
        escalation = Escalation.model_validate(payload)
    except (ValidationError, ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": f"invalid escalation: {exc}"}, status_code=400)
    store: EscalationStore = _store()
    await store.add(escalation)
    return JSONResponse({"ok": True, "escalation": escalation.to_dict()}, status_code=201)


@router.post("/escalations/{esc_id}/resolve")
async def resolve(
    esc_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark an escalation as resolved. Requires auth (mutates state)."""
    await resolve_escalation(esc_id, resolved_by=user.email)
    return RedirectResponse(url="/escalations", status_code=303)


@router.post("/api/escalations/{esc_id}/resolve")
async def resolve_api(
    esc_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    """Mark an escalation as resolved (JSON). Requires auth (mutates state)."""
    ok = await resolve_escalation(esc_id, resolved_by=user.email)
    if not ok:
        return {"ok": False, "error": f"escalation {esc_id} not found or already resolved"}
    return {"ok": True, "escalation": esc_id}


def _store() -> EscalationStore:
    """Return the cockpit's escalation store set up by ``create_app``."""
    from kai.agent.tools.escalate import _DYN

    return _DYN.store
