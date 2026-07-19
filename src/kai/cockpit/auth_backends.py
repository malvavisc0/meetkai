"""Authentication provider seam.

v1 ships MagicLinkProvider (request→approve flow). A future OIDCProvider
plugs in behind the same AuthProvider interface.
"""

from abc import ABC, abstractmethod

from sqlalchemy.orm import Session

from kai.cockpit.models import LoginToken
from kai.cockpit.tokens import (
    consume_login_token,
    create_login_token,
    fulfill_request,
    get_pending_request,
)


class AuthProvider(ABC):
    """Authentication seam. v1: MagicLinkProvider. Future: OIDCProvider."""

    @abstractmethod
    def initiate_login(self, user_id: int) -> LoginToken:
        """Create a LoginToken for the user. Returns the token."""
        ...

    @abstractmethod
    def consume_login(self, token: str) -> int | None:
        """Consume a single-use token. Returns user_id or None."""
        ...


class MagicLinkProvider(AuthProvider):
    """Request-gated magic link provider.

    initiate_login() is called ONLY by ``request approve`` (CLI or admin),
    which requires a pending LoginRequest to exist.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def initiate_login(self, user_id: int) -> LoginToken:
        """Mint a token and fulfill the pending request."""
        req = get_pending_request(self.db, user_id)
        if req is None:
            raise ValueError("No pending login request for this Operator.")
        token = create_login_token(self.db, user_id)
        fulfill_request(self.db, req.id, token.token)
        return token

    def consume_login(self, token: str) -> int | None:
        """Consume a single-use token. Returns user_id or None."""
        return consume_login_token(self.db, token)
