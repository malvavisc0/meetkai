"""Cockpit liveness route: ``GET /health``."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    """Liveness probe for the cockpit process itself."""
    return {"status": "ok"}
