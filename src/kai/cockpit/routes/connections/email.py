"""Email (Resend) Inbox connection routes: /connections/resend — CRUD + test."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from kai.cockpit.app import templates
from kai.cockpit.auth import require_user
from kai.cockpit.cli_helpers import public_url
from kai.cockpit.connections.email import EmailConnectionsService
from kai.cockpit.db import get_db
from kai.cockpit.flash import flash, flash_connection_save
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
    # Externally-facing address Resend will POST to: prefer
    # configured URL, fall back to request base URL for local dev.
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
        flash(request, "error", f"Could not save: {exc}")
    return RedirectResponse("/connections/resend", status_code=302)


@router.post("/connections/resend/delete")
async def resend_delete(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = EmailConnectionsService(db)
    svc.delete(user)
    flash(request, "success", "Email connection removed.")
    return RedirectResponse("/connections/resend", status_code=302)


@router.post("/connections/resend/test")
def resend_test(
    request: Request,
    signing_secret: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Test: sign a sample payload and POST to our
    own ingress. Sync def because ``svc.test()``
    blocks the event loop.
    """
    svc = EmailConnectionsService(db)
    ok, msg = svc.test(
        user, base_url=str(request.base_url), signing_secret=signing_secret.strip() or None
    )
    flash(request, "success" if ok else "error", f"Test {'succeeded' if ok else 'failed'}: {msg}")
    return RedirectResponse("/connections/resend", status_code=302)
