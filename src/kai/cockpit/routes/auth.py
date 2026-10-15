"""Auth routes: /login, /login/auth (magic link), /logout."""

import logging
import time
from collections import deque
from threading import Lock

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
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
from kai.cockpit.settings import get_cockpit_settings

logger = logging.getLogger(__name__)

router = APIRouter()

# Simple in-memory sliding-window rate limit for login requests. The cockpit is
# a single-process server-rendered app, so a process-local limiter is sufficient
# to blunt magic-link request spam without adding a dependency.
LOGIN_WINDOW_SECONDS = 300
LOGIN_MAX_ATTEMPTS = 5


class LoginRateLimiter:
    """Sliding-window limiter keyed by client, encapsulated so no bare mutable
    module-level state is exposed."""

    def __init__(self, window_seconds: int, max_attempts: int) -> None:
        self._window_seconds = window_seconds
        self._max_attempts = max_attempts
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow(self, client: str, now: float) -> bool:
        with self._lock:
            hits = self._hits.setdefault(client, deque())
            while hits and now - hits[0] > self._window_seconds:
                hits.popleft()
            if len(hits) >= self._max_attempts:
                return False
            hits.append(now)
            return True


LOGIN_RATE_LIMITER = LoginRateLimiter(LOGIN_WINDOW_SECONDS, LOGIN_MAX_ATTEMPTS)


def enforce_login_rate_limit(request: Request) -> None:
    if get_cockpit_settings().cockpit_testing:
        return
    client = request.client.host if request.client else "unknown"
    if not LOGIN_RATE_LIMITER.allow(client, time.monotonic()):
        raise HTTPException(status_code=429, detail="Too many login requests. Try again later.")


def auto_approve_enabled() -> bool:
    return get_cockpit_settings().cockpit_auto_approve_login


@router.get("/login")
async def login_get(request: Request, user: User | None = Depends(get_current_user)):
    if user:
        return RedirectResponse("/console", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"user": None, "requested": False})


@router.post("/login")
async def login_post(
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    enforce_login_rate_limit(request)
    user = db.query(User).filter(User.email == email).first()
    if user and not user.is_disabled:
        req = tokens.create_login_request(db, user.id)
        if auto_approve_enabled() and req is not None:
            provider = MagicLinkProvider(db)
            token = provider.initiate_login(user.id)
            magic_url = build_magic_link_url(token.token)
            background_tasks.add_task(send_magic_link, email, magic_url)
    # Always render the same "requested" state regardless of whether the email
    # maps to an existing, enabled user — never leak allowlist membership.
    return templates.TemplateResponse(request, "login.html", {"user": None, "requested": True})


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
