"""Email (Resend) Inbox connection routes: /connections/resend — CRUD + test."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from kai.cockpit.app import templates
from kai.cockpit.auth import require_user
from kai.cockpit.cli_helpers import public_url
from kai.cockpit.connection_probe import flash_connection_save
from kai.cockpit.db import get_db
from kai.cockpit.email_connections import EmailConnectionsService
from kai.cockpit.models import User

router = APIRouter()


@router.get("/connections/resend")
async def resend_page(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = EmailConnectionsService(db)
    conn = svc.get(user)
    has_secret = bool(conn and conn.config.get("signing_secret"))
    has_api_key = bool(conn and conn.config.get("api_key"))
    flash = request.session.pop("flash", None)
    # Externally-facing address Resend will POST to: prefer the configured
    # public URL (correct behind proxies/DNS), fall back to the request's own
    # base URL so local dev without KAI_PUBLIC_URL still shows something usable.
    base = public_url() or str(request.base_url).rstrip("/")
    return templates.TemplateResponse(
        request,
        "email_connection.html",
        {
            "user": user,
            "conn": conn,
            "has_secret": has_secret,
            "has_api_key": has_api_key,
            "webhook_url": f"{base}/webhook/{user.kai_slug}/resend",
            "flash": flash,
        },
    )


@router.post("/connections/resend")
def resend_save(
    request: Request,
    signing_secret: str = Form(""),
    api_key: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = EmailConnectionsService(db)
    try:
        conn = svc.save(user, signing_secret=signing_secret.strip(), api_key=api_key.strip())
        flash_connection_save(request, "Email", conn)
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        request.session["flash"] = f"Could not save: {exc}"
    return RedirectResponse("/connections/resend", status_code=302)


@router.post("/connections/resend/delete")
async def resend_delete(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = EmailConnectionsService(db)
    svc.delete(user)
    request.session["flash"] = "Email connection removed."
    return RedirectResponse("/connections/resend", status_code=302)


@router.post("/connections/resend/test")
def resend_test(
    request: Request,
    signing_secret: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Self-loopback test: sign a sample payload and POST to our own ingress.

    Accepts the same form field as save, so the operator can test a
    freshly-typed secret before saving it. When the field is blank (the
    ``••••••••`` placeholder), falls back to the persisted secret.

    Intentionally a sync ``def``, not ``async def`` like the save/delete
    routes: ``svc.test()`` makes a real HTTP call with a timeout, and a sync
    route handler runs in FastAPI's worker thread pool instead of blocking
    the event loop.
    """
    svc = EmailConnectionsService(db)
    ok, msg = svc.test(
        user, base_url=str(request.base_url), signing_secret=signing_secret.strip() or None
    )
    request.session["flash"] = f"Test {'succeeded' if ok else 'failed'}: {msg}"
    return RedirectResponse("/connections/resend", status_code=302)
