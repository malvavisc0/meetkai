"""Session middleware and auth dependencies for the cockpit web app."""

import os

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session
from starlette.requests import Request

from kai.cockpit.db import get_db
from kai.cockpit.models import User


def get_cockpit_secret() -> str:
    """Return the session signing secret from env."""
    secret = os.environ.get("KAI_COCKPIT_SECRET", "")
    if not secret:
        raise RuntimeError("KAI_COCKPIT_SECRET is not set — required for session signing")
    return secret


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """Extract user_id from session cookie, load User from DB."""
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    return (
        db.query(User)
        .filter(User.id == user_id, User.is_disabled == False)  # noqa: E712
        .first()
    )


def require_user(
    user: User | None = Depends(get_current_user),
) -> User:
    """FastAPI dependency that redirects to /login if no session."""
    if user is None:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user
