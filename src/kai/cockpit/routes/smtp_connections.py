"""SMTP connection routes: /connections/smtp — CRUD + test."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from kai.cockpit.app import templates
from kai.cockpit.auth import require_user
from kai.cockpit.connection_probe import flash_connection_save
from kai.cockpit.db import get_db
from kai.cockpit.models import User
from kai.cockpit.smtp_connections import SmtpConnectionsService

router = APIRouter()


@router.get("/connections/smtp")
async def smtp_page(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = SmtpConnectionsService(db)
    conn = svc.get(user)
    has_password = bool(conn and conn.config.get("password"))
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(
        request,
        "smtp_connection.html",
        {
            "user": user,
            "conn": conn,
            "has_password": has_password,
            "flash": flash,
        },
    )


@router.post("/connections/smtp")
def smtp_save(
    request: Request,
    host: str = Form(...),
    port: int = Form(...),
    username: str = Form(...),
    password: str = Form(""),
    from_address: str = Form(...),
    use_tls: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = SmtpConnectionsService(db)
    try:
        conn = svc.save(
            user,
            host=host.strip(),
            port=int(port),
            username=username.strip(),
            password=password,
            from_address=from_address.strip(),
            use_tls=use_tls == "true",
        )
        flash_connection_save(request, "SMTP", conn)
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        request.session["flash"] = f"Could not save: {exc}"
    return RedirectResponse("/connections/smtp", status_code=302)


@router.post("/connections/smtp/delete")
async def smtp_delete(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = SmtpConnectionsService(db)
    svc.delete(user)
    request.session["flash"] = "SMTP connection removed."
    return RedirectResponse("/connections/smtp", status_code=302)


@router.post("/connections/smtp/test")
def smtp_test(
    request: Request,
    host: str = Form(""),
    port: int = Form(0),
    username: str = Form(""),
    password: str = Form(""),
    use_tls: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Test the SMTP connection. Accepts the same form fields as save,
    so the operator can test a freshly-typed config before saving it.
    Ad-hoc mode is gated on the password field only — the template
    pre-fills plaintext fields for existing connections, so checking
    those would always trigger ad-hoc mode and bypass the persisted
    password. When password is blank, tests the persisted config.

    Intentionally a sync ``def``, not ``async def`` like the rest of this
    file: the SMTP handshake below blocks for several seconds, and a sync
    route handler is what makes FastAPI run it in its worker thread pool
    instead of blocking the single event loop.
    """
    svc = SmtpConnectionsService(db)
    if password:
        ok, msg = svc.test(
            user,
            host=host.strip() or None,
            port=port or None,
            username=username.strip() or None,
            password=password,
            use_tls=(use_tls == "true") if use_tls else None,
        )
    else:
        ok, msg = svc.test(user)
    request.session["flash"] = f"Test {'succeeded' if ok else 'failed'}: {msg}"
    return RedirectResponse("/connections/smtp", status_code=302)
