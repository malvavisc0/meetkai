"""Auth routes: /login, /login/auth (magic link), /logout."""

import os

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from kai.cockpit import tokens
from kai.cockpit.app import templates
from kai.cockpit.auth import get_current_user
from kai.cockpit.auth_backends import MagicLinkProvider
from kai.cockpit.cli_helpers import build_magic_link_url
from kai.cockpit.db import get_db
from kai.cockpit.mailer import send_magic_link
from kai.cockpit.models import User

router = APIRouter()


def _auto_approve_enabled() -> bool:
    return os.environ.get("KAI_COCKPIT_AUTO_APPROVE_LOGIN", "").lower() in ("1", "true", "yes")


@router.get("/login")
async def login_get(request: Request, user: User | None = Depends(get_current_user)):
    if user:
        return RedirectResponse("/console", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"user": None, "requested": False})


@router.post("/login")
async def login_post(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == email).first()
    requested = False
    if user and not user.is_disabled:
        req = tokens.create_login_request(db, user.id)
        if _auto_approve_enabled() and req is not None:
            provider = MagicLinkProvider(db)
            token = provider.initiate_login(user.id)
            magic_url = build_magic_link_url(token.token)
            send_magic_link(email, magic_url)
        requested = True
    return templates.TemplateResponse(request, "login.html", {"user": None, "requested": requested})


@router.get("/login/auth")
async def login_auth(
    request: Request,
    token: str,
    db: Session = Depends(get_db),
):
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
    return RedirectResponse("/console", status_code=302)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)
