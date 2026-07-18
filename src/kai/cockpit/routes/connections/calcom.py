"""Cal.com connection routes: /connections/calcom — CRUD + test."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from kai.cockpit.app import templates
from kai.cockpit.auth import require_user
from kai.cockpit.connections.calcom import CalcomConnectionsService
from kai.cockpit.db import get_db
from kai.cockpit.flash import flash, flash_connection_save
from kai.cockpit.models import User

router = APIRouter()


@router.get("/connections/calcom")
async def calcom_page(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = CalcomConnectionsService(db)
    conn = svc.get(user)
    has_key = bool(conn and conn.config.get("api_key"))
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(
        request,
        "calcom_connection.html",
        {
            "user": user,
            "conn": conn,
            "has_key": has_key,
            "flash": flash,
        },
    )


@router.post("/connections/calcom")
def calcom_save(
    request: Request,
    api_key: str = Form(""),
    base_url: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = CalcomConnectionsService(db)
    try:
        conn = svc.save(user, api_key=api_key.strip(), base_url=base_url.strip())
        flash_connection_save(request, "Cal.com", conn)
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        flash(request, "error", f"Could not save: {exc}")
    return RedirectResponse("/connections/calcom", status_code=302)


@router.post("/connections/calcom/delete")
async def calcom_delete(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = CalcomConnectionsService(db)
    svc.delete(user)
    flash(request, "success", "Cal.com connection removed.")
    return RedirectResponse("/connections/calcom", status_code=302)


@router.post("/connections/calcom/test")
def calcom_test(
    request: Request,
    api_key: str = Form(""),
    base_url: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Test the Cal.com connection. Accepts the same form fields as save,
    so the operator can test a freshly-typed key before saving it. When the
    api_key field is blank (the ``••••••••`` placeholder), falls back to the
    persisted key.

    Intentionally a sync ``def``, not ``async def`` like the rest of this
    file: ``svc.test()`` opens a real network connection with a timeout of
    several seconds, and a sync route handler is what makes FastAPI run it
    in its worker thread pool instead of blocking the single event loop.
    """
    svc = CalcomConnectionsService(db)
    ok, msg = svc.test(user, api_key=api_key.strip() or None, base_url=base_url.strip() or None)
    flash(request, "success" if ok else "error", f"Test {'succeeded' if ok else 'failed'}: {msg}")
    return RedirectResponse("/connections/calcom", status_code=302)
