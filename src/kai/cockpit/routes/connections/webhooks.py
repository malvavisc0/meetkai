"""Cockpit webhook ingress: verify signature, dedup by nonce,
forward normalized events to bot /ingest.

Unauthenticated — 404 (not 401) for unknown type/slug/conn so
attackers can't enumerate.

Replay protection is split: ``verify_signature`` checks the timestamp window
only; nonce dedup is owned by this route. The nonce is recorded ONLY after a
successful forward — a transient bot failure (502) leaves it unrecorded so the
provider's retry of the same id gets a clean re-forward attempt.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from kai.bots.waha.webhook import _MAX_BODY_BYTES
from kai.cockpit.bots import BOT_TYPES
from kai.cockpit.connections.secrets import decrypt_config
from kai.cockpit.connections.webhooks import (
    WEBHOOK_TYPES,
    WebhookUpstreamError,
    is_nonce_seen,
    record_nonce,
)
from kai.cockpit.db import get_db
from kai.cockpit.deployments import DeploymentsService
from kai.cockpit.models import Connection, Deployment, User

router = APIRouter()


@router.post("/webhook/{workspace_slug}/{type_name}")
async def webhook_ingest(
    workspace_slug: str,
    type_name: str,
    request: Request,
    db: Session = Depends(get_db),
):
    # Body-size cap — reject before allocating the body.
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")

    # Look up the webhook type.
    wh_type = WEBHOOK_TYPES.get(type_name)
    if wh_type is None:
        raise HTTPException(status_code=404, detail="not found")

    # Resolve the operator by slug.
    user = db.query(User).filter(User.kai_slug == workspace_slug).first()
    if user is None:
        raise HTTPException(status_code=404, detail="not found")

    # Load the per-operator connection row for this type.
    conn = (
        db.query(Connection)
        .filter(Connection.user_id == user.id, Connection.service == type_name)
        .first()
    )
    if conn is None:
        raise HTTPException(status_code=404, detail="not found")

    # Decrypt the per-operator signing secret.
    cfg = decrypt_config(type_name, conn.config)
    secret = cfg.get("signing_secret", "")

    # Verify the signature (401 if bad).
    if not wh_type.verify_signature(request, body, secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    # Nonce dedup — a seen nonce means the provider is retrying an already-delivered event.
    nonce = request.headers.get(wh_type.nonce_header, "") if wh_type.nonce_header else ""
    if nonce and is_nonce_seen(nonce):
        return JSONResponse({"deduped": True}, status_code=202)

    # Parse the payload into a NormalizedMessage; pass cfg through for provider-specific secrets.
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        raise HTTPException(status_code=400, detail="malformed body")
    try:
        normalized = wh_type.parse(payload, cfg)
    except WebhookUpstreamError as exc:
        raise HTTPException(status_code=502, detail=f"upstream provider API error: {exc}")
    except Exception:
        raise HTTPException(status_code=400, detail="malformed payload")

    # Find a running deployment that consumes this webhook type and forward.
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
    # forward_event is sync — offload to worker thread so the event loop isn't blocked.
    accepted = await asyncio.to_thread(
        DeploymentsService(db).forward_event, matched, "/ingest", forward_body
    )
    if not accepted:
        raise HTTPException(status_code=502, detail="bot not reachable or rejected the event")

    # Record the nonce only after a successful forward — transient bot failure leaves it unrecorded.
    if nonce:
        record_nonce(nonce)

    return JSONResponse({"ok": True}, status_code=202)
