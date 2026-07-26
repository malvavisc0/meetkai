"""Connection routes: the shared /connections page."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from kai.cockpit.app import templates
from kai.cockpit.auth import require_user
from kai.cockpit.connections.calcom import CalcomConnectionsService
from kai.cockpit.connections.database import DatabaseConnectionsService
from kai.cockpit.connections.email import EmailConnectionsService
from kai.cockpit.connections.smtp import SmtpConnectionsService
from kai.cockpit.db import get_db
from kai.cockpit.models import User

router = APIRouter()


@router.get("/connections")
async def connections_page(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
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
            "has_database": has_database,
            "has_smtp": has_smtp,
            "has_calcom": has_calcom,
            "has_resend": has_resend,
            "flash": flash,
        },
    )
