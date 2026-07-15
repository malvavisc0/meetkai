"""Deployment routes: wizard, detail, lifecycle, settings, chats, history.

Split into one module per concern (see each sub-module's docstring) because
the original single ``deployments.py`` file had grown to ~780 lines mixing
unrelated surfaces. ``router`` here is the combined router the app mounts —
callers (``kai.cockpit.app``) don't need to know about the split.
"""

from __future__ import annotations

from fastapi import APIRouter

from kai.cockpit.routes.deployments import chats, detail, history, lifecycle, settings, wizard

router = APIRouter()
router.include_router(wizard.router)
router.include_router(detail.router)
router.include_router(settings.router)
router.include_router(chats.router)
router.include_router(history.router)
router.include_router(lifecycle.router)

__all__ = ["router"]
