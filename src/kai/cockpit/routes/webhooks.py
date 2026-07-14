"""Cockpit-level webhook ingress route.

A single ``POST /webhook/{workspace_slug}/{type}`` route receives inbound
provider webhooks, verifies the signature per the type's scheme, parses the
payload, resolves the operator's running deployment that consumes that webhook
type, and forwards the normalized event to the bot's ``/ingest`` route.

Unauthenticated — this is a provider webhook, not an operator-facing page.
404 (not 401) for unknown type, unknown slug, or no connection row so an
attacker can't enumerate valid slugs/types. Signature failure for a known
type + known slug is 401.

Replay protection is split: ``verify_signature`` checks the timestamp window
only (no nonce recording); the route owns the nonce dedup set via
``is_nonce_seen``/``record_nonce``. A nonce is recorded ONLY after a
successful forward, so a transient bot failure (502) leaves it unrecorded
and the provider's retry of the same id gets a clean re-forward attempt.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from kai.bots.waha.webhook import _MAX_BODY_BYTES
from kai.cockpit.bots import BOT_TYPES
from kai.cockpit.db import get_db
from kai.cockpit.deployments import DeploymentsService
from kai.cockpit.models import Connection, Deployment, User
from kai.cockpit.secrets import decrypt_config
from kai.cockpit.webhooks import (
    WEBHOOK_TYPES,
    WebhookUpstreamError,
    is_nonce_seen,
    record_nonce,
)

router = APIRouter()


@router.post("/webhook/{workspace_slug}/{type_name}")
async def webhook_ingest(
    workspace_slug: str,
    type_name: str,
    request: Request,
    db: Session = Depends(get_db),
):
    # 1. Body-size cap — check the content-length header first (reject before
    # allocating the body), then keep a post-read backstop for a missing/lying
    # header. Runs before every other check so an oversized payload never
    # reaches verification or the DB.
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")

    # 2. Look up the webhook type (404 if unknown).
    wh_type = WEBHOOK_TYPES.get(type_name)
    if wh_type is None:
        raise HTTPException(status_code=404, detail="not found")

    # 3. Resolve the operator by slug (404 if unknown).
    user = db.query(User).filter(User.kai_slug == workspace_slug).first()
    if user is None:
        raise HTTPException(status_code=404, detail="not found")

    # 4. Load the per-operator connection row for this type (404 if none).
    conn = (
        db.query(Connection)
        .filter(Connection.user_id == user.id, Connection.service == type_name)
        .first()
    )
    if conn is None:
        raise HTTPException(status_code=404, detail="not found")

    # 5. Decrypt the per-operator signing secret.
    cfg = decrypt_config(type_name, conn.config)
    secret = cfg.get("signing_secret", "")

    # 6. Verify the signature against the decrypted secret (401 if bad).
    if not wh_type.verify_signature(request, body, secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    # 7. Nonce dedup — a seen nonce means the provider is retrying an event
    # already delivered; answer 202 (already handled, not an attack). The nonce
    # header name is owned by the WebhookType so the route stays generic.
    nonce = request.headers.get(wh_type.nonce_header, "") if wh_type.nonce_header else ""
    if nonce and is_nonce_seen(nonce):
        return JSONResponse({"deduped": True}, status_code=202)

    # 8. Parse the payload into a NormalizedMessage. ``cfg`` (already
    # decrypted above) is passed through so a provider's parse can pull
    # whatever extra secret it declared (e.g. Resend's ``api_key``, used to
    # fetch the email body — the webhook itself carries no body/attachment
    # content) without a route-side special case per provider.
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

    # 9. Find a running deployment that consumes this webhook type and forward.
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
    # forward_event is sync (uses sync httpx with a 30s timeout) — offload
    # to a worker thread so the async event loop isn't blocked for the
    # duration of the bot HTTP call. Other inbound webhooks keep flowing.
    accepted = await asyncio.to_thread(
        DeploymentsService(db).forward_event, matched, "/ingest", forward_body
    )
    if not accepted:
        raise HTTPException(status_code=502, detail="bot not reachable or rejected the event")

    # 10. Record the nonce ONLY after a successful forward — a transient bot
    # failure (502) leaves it unrecorded so the provider's retry re-forwards.
    if nonce:
        record_nonce(nonce)

    return JSONResponse({"ok": True}, status_code=202)
