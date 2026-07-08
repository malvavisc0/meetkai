"""Cockpit webapp — FastAPI app factory, middleware, route mounting."""

import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from kai.cockpit.auth import get_cockpit_secret
from kai.cockpit.db import create_all

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
logger = logging.getLogger(__name__)

_MEDIA_SERVICES: list = []  # module-level, so _lifespan shutdown can reach it


def _reconcile_deployments_in_background() -> None:
    # Bot subprocesses are children of this process (see
    # DeploymentsService.start()), so a container restart/recreation kills
    # all of them even though each deployment's `desired_state` is still
    # "running" in the DB. Restart those in a background thread so a
    # slow/failing bot start doesn't delay uvicorn from binding and
    # serving the rest of the app.
    from kai.cockpit.db import SessionLocal
    from kai.cockpit.deployments import reconcile_deployments
    from kai.cockpit.media_services import MediaServiceManager
    from kai.cockpit.tokens import cleanup_expired_tokens
    from kai.vendors.manager import get_vendor_manager

    # Start shared media services BEFORE reconcile.  start_all() blocks
    # until every enabled service is healthy and sets MEDIA_READY, so
    # reconcile only spawns bots once STT/TTS are up.
    try:
        from kai.bots.waha.config import get_waha_settings

        media = MediaServiceManager(get_waha_settings(), get_vendor_manager())
        media.start_all()
        _MEDIA_SERVICES.append(media)
    except Exception:
        logger.exception("media services failed to start; MEDIA_READY stays unset")

    db = SessionLocal()
    try:
        try:
            cleanup_expired_tokens(db)
        except Exception:
            logger.exception("startup token cleanup failed")
        try:
            reconcile_deployments()
        except Exception:
            logger.exception("startup reconciliation failed")
    finally:
        db.close()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Skipped under pytest: tests bind an isolated in-memory SQLite engine
    # via a StaticPool (single shared connection) *after* create_app() has
    # already run, and a background thread racing that connection against
    # the test's own session corrupts SQLAlchemy's identity map. Production
    # runs (KAI_COCKPIT_TESTING unset) always reconcile on startup.
    if not os.environ.get("KAI_COCKPIT_TESTING"):
        threading.Thread(
            target=_reconcile_deployments_in_background,
            name="reconcile-deployments",
            daemon=True,
        ).start()
    yield
    for media in _MEDIA_SERVICES:
        media.stop_all()


def create_app() -> FastAPI:
    app = FastAPI(title="kai cockpit", lifespan=_lifespan)

    app.add_middleware(SessionMiddleware, secret_key=get_cockpit_secret())

    # Serve self-hosted CSS, icons, and fonts. The cockpit is intentionally
    # a no-JavaScript server-rendered app — all static assets are vendored
    # locally (no runtime third-party CDN/fonts request).
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )

    create_all()

    from kai.cockpit.routes import auth, brain, chat, connections, dashboard, deployments

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(deployments.router)
    app.include_router(connections.router)
    app.include_router(brain.router)
    app.include_router(chat.router)

    return app
