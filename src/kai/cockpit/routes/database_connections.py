"""Database connection routes: /connections/database — CRUD + test."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from kai.cockpit.app import templates
from kai.cockpit.auth import require_user
from kai.cockpit.database_connections import DatabaseConnectionsService
from kai.cockpit.db import get_db
from kai.cockpit.models import User

router = APIRouter()


@router.get("/connections/database")
async def db_page(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DatabaseConnectionsService(db)
    conn = svc.get(user)
    has_url = bool(conn and conn.config.get("url"))
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(
        request,
        "database_connection.html",
        {
            "user": user,
            "conn": conn,
            "has_url": has_url,
            "flash": flash,
        },
    )


@router.post("/connections/database")
async def db_save(
    request: Request,
    label: str = Form(...),
    url: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DatabaseConnectionsService(db)
    try:
        svc.save(user, label=label.strip(), url=url.strip())
        request.session["flash"] = "Database connection saved."
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        request.session["flash"] = f"Could not save: {exc}"
    return RedirectResponse("/connections/database", status_code=302)


@router.post("/connections/database/delete")
async def db_delete(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DatabaseConnectionsService(db)
    svc.delete(user)
    request.session["flash"] = "Database connection removed."
    return RedirectResponse("/connections/database", status_code=302)


@router.post("/connections/database/test")
def db_test(
    request: Request,
    label: str = Form(""),
    url: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Test the database connection. Accepts the same form fields as save,
    so the operator can test a freshly-typed DSN before saving it. When the
    url field is blank (the ``••••••••`` placeholder), falls back to the
    persisted DSN.

    Intentionally a sync ``def``, not ``async def`` like the rest of this
    file: ``svc.test()`` opens a real network connection with a timeout of
    several seconds, and a sync route handler is what makes FastAPI run it
    in its worker thread pool instead of blocking the single event loop.
    """
    svc = DatabaseConnectionsService(db)
    ok, msg = svc.test(user, url=url.strip() or None)
    request.session["flash"] = f"Test {'succeeded' if ok else 'failed'}: {msg}"
    return RedirectResponse("/connections/database", status_code=302)
