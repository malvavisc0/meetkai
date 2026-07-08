"""Auth routes: /login, /auth/magic, /logout."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from kai.cockpit import tokens
from kai.cockpit.app import templates
from kai.cockpit.auth import get_current_user
from kai.cockpit.db import get_db
from kai.cockpit.models import User

router = APIRouter()


@router.get("/login")
async def login_get(request: Request, user: User | None = Depends(get_current_user)):
    if user:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"user": None, "requested": False})


@router.post("/login")
async def login_post(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == email).first()
    if user and not user.is_disabled:
        tokens.create_login_request(db, user.id)
    return templates.TemplateResponse(request, "login.html", {"user": None, "requested": True})


@router.get("/auth/magic")
async def auth_magic(
    request: Request,
    token: str,
    db: Session = Depends(get_db),
):
    from kai.cockpit.auth_backends import MagicLinkProvider

    provider = MagicLinkProvider(db)
    user_id = provider.consume_login(token)
    if user_id is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "user": None,
                "requested": False,
                "error": "Invalid or expired token.",
            },
        )
    request.session["user_id"] = user_id
    return RedirectResponse("/", status_code=302)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)
