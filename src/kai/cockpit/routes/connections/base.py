"""Connection routes: /connections, connect/disconnect/qr/refresh."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from kai.cockpit.app import templates
from kai.cockpit.auth import require_user
from kai.cockpit.connections.calcom import CalcomConnectionsService
from kai.cockpit.connections.database import DatabaseConnectionsService
from kai.cockpit.connections.email import EmailConnectionsService
from kai.cockpit.connections.service import ConnectionsService
from kai.cockpit.connections.smtp import SmtpConnectionsService
from kai.cockpit.db import get_db
from kai.cockpit.flash import flash
from kai.cockpit.models import User

router = APIRouter()


@router.get("/connections")
async def connections_page(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = ConnectionsService(db)
    # Re-probe WAHA when cached status is stale,
    # so phone-side disconnects surface without manual refresh.
    conn = await svc.refresh_status_if_stale(user)
    qr_url = None
    if conn and conn.status == "connecting":
        qr_url = "/connections/whatsapp/qr"

    db_conn = DatabaseConnectionsService(db).get(user)
    has_database = bool(db_conn and db_conn.config.get("url"))
    smtp_conn = SmtpConnectionsService(db).get(user)
    has_smtp = bool(smtp_conn and smtp_conn.config.get("password"))
    calcom_conn = CalcomConnectionsService(db).get(user)
    has_calcom = bool(calcom_conn and calcom_conn.config.get("api_key"))
    email_conn = EmailConnectionsService(db).get(user)
    has_resend = bool(
        email_conn and email_conn.config.get("signing_secret") and email_conn.config.get("api_key")
    )

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(
        request,
        "connections.html",
        {
            "user": user,
            "conn": conn,
            "qr_url": qr_url,
            "has_database": has_database,
            "has_smtp": has_smtp,
            "has_calcom": has_calcom,
            "has_resend": has_resend,
            "flash": flash,
        },
    )


@router.post("/connections/whatsapp/connect")
async def whatsapp_connect(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = ConnectionsService(db)
    try:
        result = await svc.connect_whatsapp(user)
        status = result.get("status", "unknown")
        if status == "connected":
            flash(request, "success", "WhatsApp connected")
        elif status == "scan_qr":
            flash(request, "info", "scan the QR code to complete connection")
        else:
            flash(request, "info", f"connection status: {status}")
    except Exception as exc:
        flash(request, "error", f"connection failed: {exc}")
    return RedirectResponse("/connections", status_code=302)


@router.get("/connections/whatsapp/qr")
async def whatsapp_qr(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = ConnectionsService(db)
    qr_bytes = await svc.get_qr(user)
    if qr_bytes:
        return Response(content=qr_bytes, media_type="image/png")
    return Response(content=b"", status_code=404)


@router.post("/connections/whatsapp/disconnect")
async def whatsapp_disconnect(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = ConnectionsService(db)
    try:
        await svc.disconnect_whatsapp(user)
        flash(request, "success", "WhatsApp disconnected")
    except Exception as exc:
        flash(request, "error", f"disconnect failed: {exc}")
    return RedirectResponse("/connections", status_code=302)


@router.post("/connections/whatsapp/refresh")
async def whatsapp_refresh(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = ConnectionsService(db)
    try:
        conn = await svc.refresh_status(user)
        flash(request, "info", f"status: {conn.status}")
    except Exception as exc:
        flash(request, "error", f"refresh failed: {exc}")
    return RedirectResponse("/connections", status_code=302)
