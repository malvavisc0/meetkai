"""Shared helpers for deployment validation, status, and history display."""

import shutil

from starlette.requests import Request

from kai.cockpit.bots import ALL_LANGUAGES, BOT_TYPES
from kai.cockpit.models import Deployment, User
from kai.runs import sanitize_instance_id


def require_supported_language(language: str) -> None:
    """Raise when ``language`` is not one of the supported languages."""
    if language not in ALL_LANGUAGES:
        raise ValueError(f"unsupported language: {language!r}. Supported: {ALL_LANGUAGES}")


def require_non_empty(value: object, label: str) -> None:
    """Raise when ``value`` is empty or whitespace-only."""
    if not value or not str(value).strip():
        raise ValueError(f"{label} is required")


def validate_create_inputs(user: User, bot_type: str, goal: str, language: str) -> None:
    """Validate the operator, bot type, goal, and language before creation."""
    if user.is_disabled:
        raise ValueError(f"Operator '{user.email}' is disabled")
    if bot_type not in BOT_TYPES:
        raise ValueError(f"unknown bot type: {bot_type}")
    require_non_empty(goal, "goal")
    require_non_empty(language, "language")
    require_supported_language(language.strip())


def kai_argv_prefix() -> list[str]:
    """Resolve the `kai` executable for spawning bot subprocesses.

    Uses the `kai` console script on PATH when available; falls back to
    ``["uv", "run", "kai"]`` for bare-metal dev checkouts.
    """
    if shutil.which("kai"):
        return ["kai"]
    return ["uv", "run", "kai"]


class ConnectionRequiredError(Exception):
    """Raised when a deployment is created or started without a required
    connection connected (see ``BotType.required_connections``)."""


class DeploymentStartupError(Exception):
    """Raised when the bot subprocess fails to start or register."""


def attention_reason(dep: Deployment, status_data: dict | None) -> str | None:
    """Why a deployment needs operator action now, or None if it doesn't.

    Triggers:

    - A ``running`` row whose live /status probe comes back empty (process
      died; reconciliation only runs at startup).
    - A running deployment with unapplied settings changes (needs_restart).

    Shared by the console list and the deployment detail page so both
    render the same verdict from the same inputs.
    """
    if dep.status == "running":
        if status_data is None:
            return "Bot process isn't responding"
        if dep.needs_restart:
            return "Restart needed to apply settings"
    return None


def instance_id_for(bot_type: str, email: str) -> str:
    """Derive the run-registry instance id from bot type and operator email."""
    return sanitize_instance_id(f"{bot_type}-{email}")


def tool_enabled(value: dict) -> bool:
    """Read the ``enabled`` flag from a stored tool toggle."""
    return bool(value.get("enabled", False))


def tool_instruction(value: dict) -> str:
    """Read the operator-supplied instruction from a stored tool toggle."""
    return str(value.get("instruction", ""))


def topbar_level(running: int, unexpected_down: int) -> str:
    """Map running and unexpected-down counts to a topbar status level."""
    if unexpected_down:
        return "down" if running == 0 else "warn"
    return "ok" if running > 0 else "warn"


def valid_msg(m: object) -> bool:
    """Report whether a stored history entry has the required message keys."""
    return isinstance(m, dict) and "role" in m and "content" in m


def normalize_msg(m: dict) -> dict:
    """Return the display shape of one stored chat message."""
    ts = m.get("ts")
    return {
        "role": str(m["role"]),
        "content": str(m["content"]),
        "ts": ts if isinstance(ts, str) else None,
    }


def topbar_status(request: Request, user: User | None) -> str:
    """Overall deployment health for the topbar indicator, shown on every page.

    Returns one of:
      - ``"none"``  — logged out, disabled account, or zero deployments
      - ``"ok"``    — no unexpected-down deployments, at least one running
      - ``"warn"``  — partial problem, or everything intentionally idle
      - ``"down"``  — nothing running, and at least one deployment that
                     should be running (per ``desired_state``) isn't
    """
    if user is None:
        return "none"

    rows = (
        request.state.db.query(Deployment.status, Deployment.desired_state)
        .filter(Deployment.user_id == user.id)
        .all()
    )

    if not rows:
        return "none"

    running = sum(1 for status, _ in rows if status == "running")
    unexpected_down = sum(
        1 for status, desired in rows if desired == "running" and status != "running"
    )
    return topbar_level(running, unexpected_down)
