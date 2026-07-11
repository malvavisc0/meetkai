"""Cockpit-level webhook ingress route.

A single ``POST /webhook/{workspace_slug}/{type}`` route receives inbound
provider webhooks, verifies the signature per the type's scheme, parses the
payload, resolves the operator's running deployment that consumes that webhook
type, and forwards the normalized event to the bot's ``/ingest`` route.

Unauthenticated — this is a provider webhook, not an operator-facing page.
404 (not 401) for unknown type or unknown slug so an attacker can't enumerate
valid slugs/types. Signature failure for a known type + known slug is 401.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from kai.cockpit.bots import BOT_TYPES
from kai.cockpit.db import get_db
from kai.cockpit.deployments import DeploymentsService
from kai.cockpit.models import Deployment, User
from kai.cockpit.webhooks import WEBHOOK_TYPES

router = APIRouter()


@router.post("/webhook/{workspace_slug}/{type_name}")
async def webhook_ingest(
    workspace_slug: str,
    type_name: str,
    request: Request,
    db: Session = Depends(get_db),
):
    wh_type = WEBHOOK_TYPES.get(type_name)
    if wh_type is None:
        raise HTTPException(status_code=404, detail="not found")

    body = await request.body()

    if not wh_type.verify_signature(request, body):
        raise HTTPException(status_code=401, detail="invalid signature")

    user = db.query(User).filter(User.kai_slug == workspace_slug).first()
    if user is None:
        raise HTTPException(status_code=404, detail="not found")

    try:
        payload = json.loads(body) if body else {}
    except Exception:
        raise HTTPException(status_code=400, detail="malformed body")

    try:
        normalized = wh_type.parse(payload)
    except Exception:
        raise HTTPException(status_code=400, detail="malformed payload")

    matched: Deployment | None = None
    for dep in (
        db.query(Deployment)
        .filter(Deployment.user_id == user.id, Deployment.status == "running")
        .all()
    ):
        bt = BOT_TYPES.get(dep.bot_type)
        if bt and (type_name in bt.required_connections or type_name in bt.supported_connections):
            matched = dep
            break
    if matched is None:
        raise HTTPException(status_code=404, detail="no running bot consumes this webhook type")

    forward_body = json.dumps(normalized.model_dump()).encode()
    accepted = DeploymentsService(db).forward_event(matched, "/ingest", forward_body)
    if not accepted:
        raise HTTPException(status_code=502, detail="bot not reachable or rejected the event")
    return Response(status_code=202)
