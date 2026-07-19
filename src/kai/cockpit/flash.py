"""Single source of truth for cockpit flash messages.

Every operator-facing write that needs a post-redirect notification goes
through :func:`flash`, which stores a structured ``{"level", "message"}``
dict in the session. ``base.html`` renders it as
``<div class="flash flash--{{ flash.level }}">{{ flash.message }}</div>``.

Do not write to ``request.session["flash"]`` directly elsewhere — that
bypasses the level and produces the single-tone bug (audit item L).
"""

from typing import Literal

from starlette.requests import Request

from kai.cockpit.models import Connection

FlashLevel = Literal["success", "info", "warn", "error"]


def flash(request: Request, level: FlashLevel, message: str) -> None:
    """Store a structured flash message for the next page render.

    ``level`` maps to a ``flash--<level>`` CSS modifier. Use:

    - ``success`` — a completed positive action (connected, created, saved, deleted).
    - ``info``    — neutral status / next-step guidance (scan QR, restart to apply).
    - ``warn``    — a soft failure the operator can recover from (could not save,
                    unknown tool, saved-but-not-verified).
    - ``error``   — a hard failure (network/HTTP error, test failed, exception).
    """
    request.session["flash"] = {"level": level, "message": message}


def flash_connection_save(request: Request, service_label: str, conn: Connection) -> None:
    """Flash the result of a connection save based on ``conn.status``.

    Shared by all connection-save routes (Cal.com, Database, Email, SMTP)
    so the success/warn wording stays in one place.
    """
    if conn.status == "connected":
        flash(request, "success", f"{service_label} connection saved and verified.")
    else:
        flash(
            request,
            "warn",
            f"{service_label} connection saved but could not be verified — "
            "use Test connection to see the error.",
        )
