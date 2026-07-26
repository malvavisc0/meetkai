"""Cockpit webhook ingress: verify signature, dedup by nonce,
forward normalized events to bot /ingest.

Unauthenticated — 404 (not 401) for unknown type/slug/conn so
attackers can't enumerate.

Replay protection is split: ``verify_signature`` checks the timestamp window
only; nonce dedup is owned by this route. The nonce is recorded ONLY after a
successful forward — a transient bot failure (502) leaves it unrecorded so the
provider's retry of the same id gets a clean re-forward attempt.
"""

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from kai.bots.webhook import _MAX_BODY_BYTES
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
    body = await _read_capped_body(request)
    wh_type, user, cfg = _resolve_webhook(db, workspace_slug, type_name, request, body)

    nonce = request.headers.get(wh_type.nonce_header, "") if wh_type.nonce_header else ""
    if nonce and is_nonce_seen(nonce):
        return JSONResponse({"deduped": True}, status_code=202)

    normalized = _parse_payload(wh_type, body, cfg)
    matched = _find_consuming_deployment(db, user.id, type_name)
    if matched is None:
        raise HTTPException(status_code=404, detail="no running bot consumes this webhook type")

    forward_body = json.dumps(normalized.model_dump()).encode()
    accepted = await asyncio.to_thread(
        DeploymentsService(db).forward_event, matched, "/ingest", forward_body
    )
    if not accepted:
        raise HTTPException(status_code=502, detail="bot not reachable or rejected the event")

    if nonce:
        record_nonce(nonce)

    return JSONResponse({"ok": True}, status_code=202)


def _resolve_webhook(
    db: Session, workspace_slug: str, type_name: str, request: Request, body: bytes
):
    """Resolve webhook type, user, connection, and verify the signature."""
    wh_type = WEBHOOK_TYPES.get(type_name)
    if wh_type is None:
        raise HTTPException(status_code=404, detail="not found")

    user = db.query(User).filter(User.kai_slug == workspace_slug).first()
    if user is None:
        raise HTTPException(status_code=404, detail="not found")

    conn = (
        db.query(Connection)
        .filter(Connection.user_id == user.id, Connection.service == type_name)
        .first()
    )
    if conn is None:
        raise HTTPException(status_code=404, detail="not found")

    cfg = decrypt_config(type_name, conn.config)
    if not wh_type.verify_signature(request, body, cfg.get("signing_secret", "")):
        raise HTTPException(status_code=401, detail="invalid signature")

    return wh_type, user, cfg


async def _read_capped_body(request: Request) -> bytes:
    """Read the request body, rejecting oversized payloads."""
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    return body


def _parse_payload(wh_type, body: bytes, cfg: dict):
    """Parse and normalize the webhook payload."""
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        raise HTTPException(status_code=400, detail="malformed body")
    try:
        return wh_type.parse(payload, cfg)
    except WebhookUpstreamError as exc:
        raise HTTPException(status_code=502, detail=f"upstream provider API error: {exc}")
    except Exception:
        raise HTTPException(status_code=400, detail="malformed payload")


def _find_consuming_deployment(db: Session, user_id: int, type_name: str) -> Deployment | None:
    """Find a running deployment that consumes this webhook type."""
    for dep in (
        db.query(Deployment)
        .filter(Deployment.user_id == user_id, Deployment.status == "running")
        .all()
    ):
        bt = BOT_TYPES.get(dep.bot_type)
        if bt and (type_name in bt.required_connections or type_name in bt.supported_connections):
            return dep
    return None
