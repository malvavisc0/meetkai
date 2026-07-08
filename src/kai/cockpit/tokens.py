"""LoginRequest + LoginToken repository functions.

All functions take an ORM Session and operate on the login_requests /
login_tokens tables. Used by both the CLI (cockpit request commands) and
the web auth routes.
"""

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from kai.cockpit.models import LoginRequest, LoginToken

TOKEN_TTL_MINUTES = 10


def create_login_request(db: Session, user_id: int) -> LoginRequest | None:
    """Create a pending login request if none exists. Returns the request or None."""
    existing = (
        db.query(LoginRequest)
        .filter(LoginRequest.user_id == user_id, LoginRequest.status == "pending")
        .first()
    )
    if existing:
        return None
    req = LoginRequest(
        user_id=user_id,
        status="pending",
        created_at=datetime.now(UTC).isoformat(),
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def get_pending_request(db: Session, user_id: int) -> LoginRequest | None:
    """Return the user's pending login request, or None."""
    return (
        db.query(LoginRequest)
        .filter(LoginRequest.user_id == user_id, LoginRequest.status == "pending")
        .first()
    )


def fulfill_request(db: Session, request_id: int, token_id: str) -> None:
    """Mark a login request as fulfilled."""
    req = db.query(LoginRequest).filter(LoginRequest.id == request_id).first()
    if req:
        req.status = "fulfilled"
        req.fulfilled_at = datetime.now(UTC).isoformat()
        req.token_id = token_id
        db.commit()


def create_login_token(db: Session, user_id: int) -> LoginToken:
    """Create a single-use login token with a 10-minute TTL."""
    now = datetime.now(UTC)
    token = LoginToken(
        token=secrets.token_urlsafe(32),
        user_id=user_id,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=TOKEN_TTL_MINUTES)).isoformat(),
    )
    db.add(token)
    db.commit()
    db.refresh(token)
    return token


def _is_expired(expires_at: str, now: datetime) -> bool:
    """True if ``expires_at`` (ISO-8601, maybe tz-naive) is in the past.

    Parses the timestamp rather than comparing strings lexicographically:
    a tz-naive or differently-formatted producer would otherwise break a
    raw string comparison. Naive timestamps are assumed UTC (the convention
    every producer in this module follows).
    """
    try:
        expires = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return now > expires


def consume_login_token(db: Session, token_str: str) -> int | None:
    """Consume a single-use token. Returns user_id or None if invalid/expired."""
    token = db.query(LoginToken).filter(LoginToken.token == token_str).first()
    if token is None:
        return None
    if token.consumed_at is not None:
        return None
    now = datetime.now(UTC)
    if _is_expired(token.expires_at, now):
        return None
    token.consumed_at = now.isoformat()
    db.commit()
    return token.user_id


def cleanup_expired_tokens(db: Session) -> int:
    """Delete expired, unconsumed tokens. Returns count deleted."""
    now = datetime.now(UTC)
    candidates = db.query(LoginToken).filter(LoginToken.consumed_at.is_(None)).all()
    expired = [t for t in candidates if _is_expired(t.expires_at, now)]
    for token in expired:
        db.delete(token)
    db.commit()
    return len(expired)
