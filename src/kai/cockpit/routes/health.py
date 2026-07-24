"""Cockpit health route: ``GET /health``.

Readiness-style probe: verifies the process is up *and* its database is
reachable, so a cockpit with a dead DB reports unhealthy instead of a static
``ok`` (which would let the container healthcheck mask a real outage).
"""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from kai.cockpit.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health(db: Session = Depends(get_db)) -> JSONResponse:
    """Liveness + DB readiness probe for the cockpit."""
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        logger.exception("Health check failed: database unreachable")
        return JSONResponse(
            {"status": "unhealthy", "database": "unreachable", "detail": str(exc)},
            status_code=503,
        )
    return JSONResponse({"status": "ok", "database": "ok"})
