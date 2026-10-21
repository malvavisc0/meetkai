"""Cockpit webapp — FastAPI app factory, middleware, route mounting."""

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from kai.cockpit.auth import get_cockpit_secret, get_current_user
from kai.cockpit.csrf import CSRF_TOKEN, CSRFMiddleware
from kai.cockpit.db import (
    SessionFactory,
    build_engine,
    build_session_factory,
    run_migrations,
)
from kai.cockpit.settings import get_cockpit_settings
from kai.observability import init_sentry

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals["csrf_token"] = lambda: CSRF_TOKEN.get() or ""
templates.env.globals["csrf_field"] = lambda: Markup(
    f'<input type="hidden" name="_csrf" value="{CSRF_TOKEN.get() or ""}">'
)
logger = logging.getLogger(__name__)

PLACEHOLDER_SECRETS = frozenset(
    {"", "change-me", "change-me-openssl-rand-hex-16", "change-me-openssl-rand-hex-32"}
)


def require_real_secret(name: str, value: str) -> None:
    """Fail fast at boot when a required secret is unset or a known placeholder."""
    if value in PLACEHOLDER_SECRETS:
        raise RuntimeError(
            f"{name} is not set to a real value (generate: openssl rand -hex 32). "
            "Set it, or run with KAI_COCKPIT_TESTING=1 for local dev."
        )


# Registered as a Jinja global (not threaded through every route's context)
# so the topbar's status dot can render the same way on every page. See
# `topbar_status()` for the cheap, no-live-probe status computation.
from kai.cockpit.deployments import topbar_status  # noqa: E402 - Jinja global registration

templates.env.globals["topbar_status"] = topbar_status


def format_timestamp(timestamp: str | datetime | None) -> str:
    """Render persisted ISO timestamps (or datetimes) in the server's local
    timezone for UI use. Accepts a string (the cockpit DB's string columns),
    a ``datetime`` (e.g. Pydantic model fields), or ``None``/empty.
    """
    if not timestamp:
        return ""
    if isinstance(timestamp, datetime):
        parsed = timestamp
    else:
        try:
            parsed = datetime.fromisoformat(str(timestamp))
        except ValueError:
            return str(timestamp)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    local = parsed.astimezone()
    timezone = local.strftime("%Z") or "local"
    return local.strftime(f"%Y-%m-%d %H:%M:%S {timezone}")


templates.env.globals["format_timestamp"] = format_timestamp


def reconcile_deployments_in_background(session_factory: SessionFactory) -> None:
    """Reconcile deployments and clean expired tokens on a startup thread."""
    # Bot subprocesses are children of this process, so a container restart
    # kills all of them even though each deployment's `desired_state` is
    # still "running" in the DB. Restart in a background thread so a
    # slow/failing bot start doesn't delay uvicorn from binding.
    from kai.cockpit.deployments import reconcile_deployments
    from kai.cockpit.tokens import cleanup_expired_tokens

    db = session_factory()
    try:
        try:
            cleanup_expired_tokens(db)
        except Exception:
            logger.exception("startup token cleanup failed")
            db.rollback()
        try:
            reconcile_deployments(db)
        except Exception:
            logger.exception("startup reconciliation failed")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run startup reconciliation and yield for the app's lifetime."""
    # Skipped under pytest: tests use an in-memory SQLite StaticPool (single
    # shared connection) and a background thread racing that connection
    # against the test's own session corrupts SQLAlchemy's identity map.
    # Production runs always reconcile on startup.
    if not get_cockpit_settings().cockpit_testing:
        reconcile_thread = threading.Thread(
            target=reconcile_deployments_in_background,
            args=(app.state.session_factory,),
            name="reconcile-deployments",
            daemon=True,
        )
        reconcile_thread.start()
    else:
        reconcile_thread = None
    try:
        yield
    finally:
        if reconcile_thread is not None:
            reconcile_thread.join(timeout=5.0)
        engine = getattr(app.state, "engine", None)
        if engine is not None and (reconcile_thread is None or not reconcile_thread.is_alive()):
            engine.dispose()


def create_app(session_factory: SessionFactory | None = None) -> FastAPI:
    """Build the cockpit FastAPI app with middleware and routes.

    ``session_factory`` lets tests and embedders inject their own database.
    Production builds an engine from ``CockpitSettings.cockpit_db``.
    """
    init_sentry(
        "cockpit",
        extra_integrations=[FastApiIntegration(), StarletteIntegration()],
        profiles_sample_rate=0.01,
    )

    settings = get_cockpit_settings()
    app = FastAPI(title="kai cockpit", lifespan=lifespan)
    if session_factory is None:
        owned_engine = build_engine()
        session_factory = build_session_factory(owned_engine)
    else:
        owned_engine = None
    app.state.engine = owned_engine
    app.state.session_factory = session_factory

    app.add_middleware(CSRFMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=get_cockpit_secret(),
        https_only=bool(settings.cookie_secure),
        same_site="lax",
        max_age=172800,
    )

    # Serve self-hosted CSS, icons, and fonts. The cockpit is a no-JavaScript
    # server-rendered app — all static assets are vendored locally.
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )

    if not settings.cockpit_testing:
        if owned_engine is not None:
            run_migrations()
        require_real_secret("KAI_COCKPIT_SECRET", settings.cockpit_secret)
        require_real_secret("KAI_COCKPIT_ESCALATION_SECRET", settings.cockpit_escalation_secret)
        from kai.cockpit.connections.secrets import get_encryption_settings

        require_real_secret(
            "KAI_CREDENTIAL_ENCRYPTION_KEY",
            get_encryption_settings().credential_encryption_key,
        )

    # Cockpit's aggregated escalation store. Bots POST to /api/escalations
    # and this store holds them so the dashboard + sidebar badge read a
    # single source of truth. Created here (not at module import) so per-test
    # redirection of KAI_ESCALATIONS_PATH is respected. The Jinja global is
    # (re)bound here for the same reason — it renders the current app's store.
    from kai.agent.tools.escalate import EscalationState, EscalationStore

    app.state.escalations = EscalationState(
        store=EscalationStore(get_cockpit_settings().escalations_path)
    )
    templates.env.globals["active_escalation_count"] = app.state.escalations.store.active_count

    from kai.cockpit.routes import (
        auth,
        brain,
        chat,
        console,
        dependencies,
        deployments,
        escalations,
        health,
    )
    from kai.cockpit.routes.connections import (
        base,
        calcom,
        database,
        email,
        smtp,
        webhooks,
    )

    app.include_router(auth.router)
    app.include_router(console.router)
    app.include_router(dependencies.router)
    app.include_router(deployments.router)
    app.include_router(base.router)
    app.include_router(database.router)
    app.include_router(email.router)
    app.include_router(calcom.router)
    app.include_router(brain.router)
    app.include_router(chat.router)
    app.include_router(escalations.router)
    app.include_router(health.router)
    app.include_router(smtp.router)
    app.include_router(webhooks.router)

    if settings.cockpit_testing:

        @app.get("/_csrf")
        async def get_csrf_token(request: Request) -> dict[str, str]:
            """Expose the session CSRF token to test clients."""
            return {"token": request.session.get("csrf_token", "")}

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
        """Render redirects, JSON, or the HTML error page for HTTP exceptions."""
        # Pass through redirects (require_user raises HTTPException(302)).
        if exc.status_code in (301, 302, 303, 307, 308):
            from starlette.responses import RedirectResponse

            redirect_headers = exc.headers or {}
            return RedirectResponse(
                url=redirect_headers.get("Location", "/"),
                status_code=exc.status_code,
                headers=redirect_headers,
            )
        # Webhook / API endpoints serve machine consumers — return JSON, not
        # the operator-facing HTML error page.
        accept = request.headers.get("accept", "")
        if request.url.path.startswith("/webhook") or "text/html" not in accept:
            from starlette.responses import JSONResponse

            return JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=dict(exc.headers) if exc.headers else None,
            )
        # Only open a DB session if the user has a session cookie — anonymous
        # 404s from bots/scanners skip the pool entirely. Route dependencies
        # have already been torn down when the error handler runs, so always
        # use (and close) a fresh session here.
        user = None
        db = None
        if request.session.get("user_id") is not None:
            db = request.app.state.session_factory()
            request.state.db = db
            try:
                user = get_current_user(request, db)
            except Exception:
                user = None
        # Render while the session is still open — base.html's topbar_status
        # reads request.state.db during template evaluation.
        try:
            return templates.TemplateResponse(
                request,
                "_error.html",
                {"user": user, "status": exc.status_code, "detail": exc.detail},
                status_code=exc.status_code,
            )
        finally:
            if db is not None:
                db.close()

    return app
