"""Shared connections service.

Provides a generic ``list_for_user`` helper used by the deployment readiness
gates and the settings form. Per-service CRUD (email/database/smtp/calcom)
lives in each service's own module under ``kai.cockpit.connections``.
"""

import logging

from sqlalchemy.orm import Session

from kai.cockpit.models import Connection, User

logger = logging.getLogger(__name__)


class ConnectionsService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_for_user(self, user: User) -> list[Connection]:
        """Every connection row for this operator (all services)."""
        return self.db.query(Connection).filter(Connection.user_id == user.id).all()
