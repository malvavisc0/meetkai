"""Deployment settings: ``GET``/``POST /deployments/{dep_id}/settings``.

Per-bot-type settings parsing is dispatched through ``_SETTINGS_PARSERS``
(mirroring ``_shared.SETTINGS_TEMPLATES``'s per-bot-type template dispatch)
instead of an ``if/elif dep.bot_type == ...`` chain growing in the POST
handler. Adding a new bot type's settings means adding a parser function
and a table entry here — the handler itself doesn't change.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from kai.agent.tools.email import DEFAULT_DISPLAY_NAME
from kai.cockpit.app import templates
from kai.cockpit.auth import require_user
from kai.cockpit.bots import BOT_TYPES, CAPABILITY_LABELS, CREDENTIAL_TYPES
from kai.cockpit.brains import BrainsService
from kai.cockpit.connections import ConnectionsService
from kai.cockpit.db import get_db
from kai.cockpit.deployments import DeploymentsService, _tool_enabled, _tool_instruction
from kai.cockpit.models import User
from kai.cockpit.routes.deployments._shared import (
    ALL_VOICES,
    SETTINGS_TEMPLATES,
    build_tools_update,
    get_deployment,
)

router = APIRouter()

# A per-bot-type settings parser takes the deployment id, request (for
# flash-message redirects on validation errors), and the text-only form
# fields, and returns either:
#   - a RedirectResponse (a validation error the operator must fix), or
#   - ``(settings_updates, voice)`` to merge into ``settings_update`` and
#     pass through to ``svc.edit()`` (``voice`` is "" for bot types with no
#     voice concept).
SettingsParseResult = tuple[dict, str] | RedirectResponse
SettingsParser = Callable[[int, Request, dict], SettingsParseResult]


def _parse_waha_settings(dep_id: int, request: Request, form_fields: dict) -> SettingsParseResult:
    """Waha-only settings: voice, triggers, chats, participation, voice map.

    The email bot has none of these — including them would pollute the
    deployment's settings dict with waha-specific defaults the email bot
    never reads, and the kokoro voice-map validation is waha-specific.
    """
    from kai.bots.waha.tts import SUPPORTED_KOKORO_LANGS, parse_voice_map

    voice = form_fields.get("voice", "")
    kokoro_voice_map = (form_fields.get("kokoro_voice_map", "") or "").strip()
    unknown_langs = sorted(
        lang for lang in parse_voice_map(kokoro_voice_map) if lang not in SUPPORTED_KOKORO_LANGS
    )
    if unknown_langs:
        request.session["flash"] = (
            f"Unknown Kokoro language code(s) in voice overrides: {', '.join(unknown_langs)}. "
            f"Supported: {', '.join(SUPPORTED_KOKORO_LANGS)}."
        )
        return RedirectResponse(f"/deployments/{dep_id}/settings", status_code=302)

    def _form_int(key: str, default: int) -> int:
        val = form_fields.get(key, str(default))
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    def _form_float(key: str, default: float) -> float:
        val = form_fields.get(key, str(default))
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    updates = {
        "trigger_keyword": form_fields.get("trigger_keyword", ""),
        "mentions_enabled": form_fields.get("mentions_enabled") == "true",
        "whitelist": [
            line.strip()
            for line in (form_fields.get("whitelist", "") or "").splitlines()
            if line.strip()
        ],
        "blacklist": [
            line.strip()
            for line in (form_fields.get("blacklist", "") or "").splitlines()
            if line.strip()
        ],
        "participation": {
            "enabled": form_fields.get("participation_enabled") == "true",
            "rate": _form_float("participation_rate", 0.15),
            "cooldown_seconds": _form_int("participation_cooldown", 90),
            "streak_max": _form_int("participation_streak_max", 2),
            "voice_note_rate": _form_float("voice_note_rate", 0.25),
            "voice_note_cooldown": _form_int("voice_note_cooldown", 300),
        },
        "kokoro_voice_map": kokoro_voice_map,
        "display_name": (form_fields.get("display_name", "") or "").strip() or DEFAULT_DISPLAY_NAME,
    }
    return updates, voice


def _parse_email_settings(dep_id: int, request: Request, form_fields: dict) -> SettingsParseResult:
    """Email-only setting: a blocklist of sender addresses to silently
    ignore in ``ingest_event``, before any attachment download or agent
    turn (see ``kai.bots.email.Bot.ingest_event``). Unlike waha's chat
    whitelist/blacklist, there's no "allow only these senders" counterpart
    — just a blocklist.
    """
    updates = {
        "blacklist": [
            line.strip().lower()
            for line in (form_fields.get("blacklist", "") or "").splitlines()
            if line.strip()
        ],
        "display_name": (form_fields.get("display_name", "") or "").strip() or DEFAULT_DISPLAY_NAME,
    }
    return updates, ""


# Bot types with no entry here (e.g. a brand-new bot type) simply get no
# extra settings parsed — ``settings_update`` keeps the shared
# timezone/tools keys only, same as before this table existed.
_SETTINGS_PARSERS: dict[str, SettingsParser] = {
    "waha": _parse_waha_settings,
    "email": _parse_email_settings,
}


@router.get("/deployments/{dep_id}/settings")
async def deployment_settings_page(
    request: Request,
    dep_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result

    bt = BOT_TYPES.get(dep.bot_type)
    # Render every flag the bot type declares, not just the entitled ones:
    # an unentitled flag shows up disabled + unchecked so the operator sees
    # what's possible and knows to request access, rather than staring at an
    # empty card. The POST handler clamps submitted values to entitlements
    # so a crafted checkbox can't self-enable anything.
    entitlements = {k for k, v in (user.feature_flags or {}).items() if v}
    feature_flags: list[tuple[str, str, bool]] = []
    if bt:
        for flag in bt.feature_flags:
            label = CAPABILITY_LABELS.get(flag, flag.capitalize())
            feature_flags.append((flag, label, flag in entitlements))

    # Build the optional-connection toggles from the catalog: one checkbox
    # per supported connection that isn't required (required ones are always
    # on, no toggle). Each is disabled when the connection doesn't exist yet
    # — the toggle is stored intent, not an executed grant.
    available_conns = {c.service for c in ConnectionsService(db).list_for_user(user)}
    supported_tools: list[tuple[str, str, bool]] = []
    if bt:
        for conn_svc in bt.supported_connections:
            if conn_svc in bt.required_connections:
                continue
            label = (
                CREDENTIAL_TYPES[conn_svc].label
                if conn_svc in CREDENTIAL_TYPES
                else conn_svc.capitalize()
            )
            supported_tools.append((conn_svc, label, conn_svc in available_conns))
    tools_enabled = dep.settings.get("tools", {})

    # Per-tool state for tools that carry an instruction (database).
    # Uses the same _tool_enabled/_tool_instruction helpers as start() so
    # there's a single source of truth for the bool→dict transition.
    tools_state: dict[str, dict] = {}
    for conn_svc, _, available in supported_tools:
        raw = tools_enabled.get(conn_svc, False)
        tools_state[conn_svc] = {
            "enabled": _tool_enabled(raw),
            "instruction": _tool_instruction(raw),
            "available": available,
        }

    flash = request.session.pop("flash", None)

    brain = BrainsService(db).get_brain(user)

    from kai.bots.waha.tts import SUPPORTED_KOKORO_LANGS

    template_name = SETTINGS_TEMPLATES.get(dep.bot_type, SETTINGS_TEMPLATES["default"])

    return templates.TemplateResponse(
        request,
        template_name,
        {
            "user": user,
            "dep": dep,
            "dep_user": user,
            "voices": ALL_VOICES,
            "kokoro_languages": SUPPORTED_KOKORO_LANGS,
            "feature_flags": feature_flags,
            "capability_labels": CAPABILITY_LABELS,
            "has_brain": brain is not None,
            "supported_tools": supported_tools,
            "tools_state": tools_state,
            "flash": flash,
            "default_display_name": DEFAULT_DISPLAY_NAME,
        },
    )


@router.post("/deployments/{dep_id}/settings")
async def deployment_settings(
    request: Request,
    dep_id: int,
    goal: str = Form(...),
    language: str = Form(...),
    timezone: str = Form(""),
    brain_mandatory: str = Form(""),
    brain_instruction: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result

    bt = BOT_TYPES.get(dep.bot_type)
    # A deployment may only enable a feature flag the user is entitled to.
    # The form renders only entitled flags, but a direct POST can spoof any
    # checkbox name — so we clamp server-side: an unentitled flag forced on
    # is silently dropped (rather than 403) to avoid leaking entitlement
    # state to a probing attacker. An entitlement is a flag whose value is
    # truthy in the user's feature_flags dict.
    entitlements = {k for k, v in (user.feature_flags or {}).items() if v}
    # This form has no file inputs, so every submitted field is text; drop
    # anything that isn't a str (e.g. UploadFile) rather than mistyping the
    # dict as dict[str, UploadFile | str] everywhere below.
    form_fields = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    feature_flags = {}
    supported_svcs: list[str] = []
    if bt:
        for flag in bt.feature_flags:
            requested = f"feature_{flag}" in form_fields
            feature_flags[flag] = bool(requested and flag in entitlements)
        supported_svcs = [
            conn_svc
            for conn_svc in bt.supported_connections
            if conn_svc not in bt.required_connections
        ]

    settings_update: dict = {
        "timezone": timezone or None,
        "tools": build_tools_update(supported_svcs, form_fields),
    }

    voice = ""
    parser = _SETTINGS_PARSERS.get(dep.bot_type)
    if parser is not None:
        parsed = parser(dep_id, request, form_fields)
        if isinstance(parsed, RedirectResponse):
            return parsed
        extra_updates, voice = parsed
        settings_update.update(extra_updates)

    try:
        svc.edit(
            dep,
            goal=goal,
            language=language,
            voice=voice or dep.voice,
            feature_flags=feature_flags,
            settings=settings_update,
            brain_mandatory=(brain_mandatory == "true"),
            brain_instruction=brain_instruction.strip() or None,
        )
    except ValueError as exc:
        request.session["flash"] = str(exc)
        return RedirectResponse(f"/deployments/{dep_id}/settings", status_code=302)

    if dep.status == "running":
        request.session["flash"] = "Settings saved. Restart to apply."
    else:
        request.session["flash"] = "Settings saved."
    return RedirectResponse(f"/deployments/{dep_id}/settings", status_code=302)
