"""Deployment routes: wizard, detail, lifecycle, settings."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from kai.bots.waha.client import WahaClient
from kai.bots.waha.config import get_waha_settings
from kai.cockpit.app import templates
from kai.cockpit.auth import require_user
from kai.cockpit.bots import (
    AGENT_ONLY_LANGUAGES,
    BOT_TYPES,
    CAPABILITY_LABELS,
    CONNECTION_LABELS,
    CREDENTIAL_TYPES,
    LANGUAGE_VOICE_MAP,
    BotType,
    auto_pick_voice,
)
from kai.cockpit.connections import ConnectionsService
from kai.cockpit.db import get_db
from kai.cockpit.deployments import (
    ConnectionRequiredError,
    DeploymentsService,
    DeploymentStartupError,
    _tool_enabled,
    _tool_instruction,
    attention_reason,
)
from kai.cockpit.models import Deployment, User

logger = logging.getLogger(__name__)

router = APIRouter()

ALL_VOICES = sorted(set(LANGUAGE_VOICE_MAP.values()))
ALL_LANGUAGES = sorted({*LANGUAGE_VOICE_MAP.keys(), *AGENT_ONLY_LANGUAGES})

# Services that carry an instruction textarea alongside the toggle.
_TOOLS_WITH_INSTRUCTION = frozenset({"database", "smtp"})

_HOME_REDIRECT = RedirectResponse("/console", status_code=302)


def _build_tools_update(supported_svcs: list[str], form_fields: dict) -> dict[str, bool | dict]:
    """Build the ``settings["tools"]`` dict from the submitted form.

    Simple toggles store a bool. Tools with an instruction (database) store
    a nested ``{"enabled": bool, "instruction": str}`` dict.
    """
    tools: dict[str, bool | dict] = {}
    for svc in supported_svcs:
        enabled = f"tool_{svc}" in form_fields
        if svc in _TOOLS_WITH_INSTRUCTION:
            instruction = form_fields.get(f"tool_{svc}_instruction", "")
            tools[svc] = {"enabled": enabled, "instruction": instruction.strip()}
        else:
            tools[svc] = enabled
    return tools


def _get_deployment(
    svc: DeploymentsService, dep_id: int, user: User
) -> tuple[DeploymentsService, Deployment] | RedirectResponse:
    """Fetch a deployment and verify ownership; return redirect on failure."""
    dep = svc.get(dep_id)
    if not dep or dep.user_id != user.id:
        return _HOME_REDIRECT
    return svc, dep


def _uptime_str(seconds: int) -> str:
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _fmt_ts(ts: str | None) -> str:
    """Render an ISO-8601 timestamp for display in the server's local timezone.

    Messages are stored as UTC-aware ISO strings (see
    ``KaiAgent._now_ts()``), which is the right way to persist them — but
    displaying that raw UTC value labeled "UTC" is misleading for a
    human reading the cockpit from the server's timezone (``TZ`` env var,
    e.g. ``Europe/Berlin``). Convert to the server's local tz for display.
    """
    if not ts:
        return ""
    try:
        parsed = datetime.fromisoformat(ts)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        local = parsed.astimezone()
        tz_label = local.strftime("%Z") or "local"
        return local.strftime(f"%Y-%m-%d %H:%M:%S {tz_label}")
    except ValueError:
        return ts


# --- Deploy wizard ---


def _missing_required_connections(db: Session, user: User, bt: BotType) -> list[str]:
    """Display labels for the ``bt.required_connections`` this operator has
    not connected yet.

    Empty list means the bot type can be created right now. Shared by the
    wizard's GET (to gate the submit button) and POST (server-side, so a
    disabled button in the DOM is never the only thing standing between an
    operator and a deployment its ``required_connections`` don't satisfy —
    ``DeploymentsService.create()`` enforces the same rule either way).
    """
    if not bt.required_connections:
        return []
    connected = {
        c.service for c in ConnectionsService(db).list_for_user(user) if c.status == "connected"
    }
    missing = [service for service in bt.required_connections if service not in connected]
    return [CONNECTION_LABELS.get(service, service) for service in missing]


@router.get("/deployments/new")
async def deploy_new_get(
    request: Request,
    bot_type: str = "waha",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    bt = BOT_TYPES.get(bot_type)
    if bt is None:
        return RedirectResponse("/console", status_code=302)

    dep_svc = DeploymentsService(db)
    existing = dep_svc.get_for_user_and_type(user.id, bot_type)
    if existing:
        return RedirectResponse(f"/deployments/{existing.id}", status_code=302)

    voice = auto_pick_voice(user.language)
    return templates.TemplateResponse(
        request,
        "deploy_wizard.html",
        {
            "user": user,
            "step": "config",
            "bot_type": bot_type,
            "bt": bt,
            "goal": bt.default_goal,
            "language": user.language,
            "voice": voice,
            "voices": ALL_VOICES,
            "languages": ALL_LANGUAGES,
            "missing_connections": _missing_required_connections(db, user, bt),
        },
    )


@router.post("/deployments/new")
async def deploy_new_post(
    request: Request,
    bot_type: str = Form("waha"),
    goal: str = Form(...),
    language: str = Form(...),
    voice: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    try:
        dep = svc.create(user, bot_type, goal, language, voice or None)
    except (ValueError, ConnectionRequiredError) as exc:
        bt = BOT_TYPES.get(bot_type)
        return templates.TemplateResponse(
            request,
            "deploy_wizard.html",
            {
                "user": user,
                "step": "config",
                "bot_type": bot_type,
                "bt": bt,
                "goal": goal,
                "language": language,
                "voice": voice,
                "voices": ALL_VOICES,
                "languages": ALL_LANGUAGES,
                "missing_connections": _missing_required_connections(db, user, bt) if bt else [],
                "error": str(exc),
            },
        )

    # Deployment created — go straight to its detail page. The detail page
    # already gates the start button on WhatsApp being connected (showing a
    # "connect WhatsApp" action instead), so the intermediate "ready" step
    # is redundant.
    request.session["flash"] = "Deployment created."
    return RedirectResponse(f"/deployments/{dep.id}", status_code=302)


# --- Deployment detail ---


@router.get("/deployments/{dep_id}")
async def deployment_detail(
    request: Request,
    dep_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = _get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result

    # A waha deployment cannot start until the user's WhatsApp Connection is
    # "connected" — the start button must be hidden (and a connect-whatsapp
    # action shown instead) when that precondition isn't met, so the operator
    # is never offered a start that deployments.start() will refuse anyway.
    conn_svc = ConnectionsService(db)
    whatsapp = conn_svc.get_whatsapp(user)
    whatsapp_connected = bool(whatsapp and whatsapp.status == "connected")

    status_data = None
    uptime_str = None
    uptime_s = None
    if dep.status == "running":
        status_data = svc.fetch_status(dep)
        started_at = svc.run_started_at(dep)
        if started_at:
            try:
                started = datetime.fromisoformat(started_at)
                if started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                delta = int((datetime.now(UTC) - started).total_seconds())
                uptime_s = max(0, delta)
                uptime_str = _uptime_str(uptime_s)
            except (ValueError, TypeError):
                pass

    # Same signal the console list badges use — a running bot whose
    # WhatsApp got disconnected out from under it looks identical to a
    # healthy one otherwise (still "running", Stop button still shown), so
    # an operator landing directly on this page (not via /console) would
    # otherwise have no way to notice messages are silently failing.
    reason = attention_reason(dep, status_data, whatsapp_connected)

    flash = request.session.pop("flash", None)
    # needs_restart is now a persisted column (survives reloads/new tabs),
    # not a session flash — see DeploymentsService.edit()/start()/stop().
    needs_restart = bool(dep.needs_restart) and dep.status == "running"

    conversation_count, message_count = svc.interaction_summary(dep)
    reply = request.session.pop("chat_reply", None)

    return templates.TemplateResponse(
        request,
        "deployment.html",
        {
            "user": user,
            "dep": dep,
            "dep_user": user,
            "status": status_data,
            "uptime_str": uptime_str,
            "uptime_s": uptime_s,
            "needs_restart": needs_restart,
            "whatsapp_connected": whatsapp_connected,
            "attention_reason": reason,
            "conversation_count": conversation_count,
            "message_count": message_count,
            "capability_labels": CAPABILITY_LABELS,
            "reply": reply,
            "flash": flash,
        },
    )


# --- Settings ---


@router.get("/deployments/{dep_id}/settings")
async def deployment_settings_page(
    request: Request,
    dep_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = _get_deployment(svc, dep_id, user)
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
        for svc in bt.supported_connections:
            if svc in bt.required_connections:
                continue
            label = CREDENTIAL_TYPES[svc].label if svc in CREDENTIAL_TYPES else svc.capitalize()
            supported_tools.append((svc, label, svc in available_conns))
    tools_enabled = dep.settings.get("tools", {})

    # Per-tool state for tools that carry an instruction (database).
    # Uses the same _tool_enabled/_tool_instruction helpers as start() so
    # there's a single source of truth for the bool→dict transition.
    tools_state: dict[str, dict] = {}
    for svc, _, available in supported_tools:
        raw = tools_enabled.get(svc, False)
        tools_state[svc] = {
            "enabled": _tool_enabled(raw),
            "instruction": _tool_instruction(raw),
            "available": available,
        }

    flash = request.session.pop("flash", None)

    from kai.cockpit.brains import BrainsService

    brain = BrainsService(db).get_brain(user)

    from kai.bots.waha.tts import SUPPORTED_KOKORO_LANGS

    return templates.TemplateResponse(
        request,
        "settings.html",
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
        },
    )


# --- Chat picker (whitelist/blacklist helper) ---


def _avatar_initial(name: str | None, chat_id: str) -> str:
    """Pick a 1–2 char avatar label for a chat row."""
    label = (name or "").strip()
    if label:
        return label[0].upper()
    return (chat_id or "?")[0].upper()


@router.get("/deployments/{dep_id}/chats.json")
async def deployment_chats_json(
    dep_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Proxy WAHA's chat overview for the chat picker on the Settings page.

    Returns ``{"chats": [{id, name, avatar_initial}], "has_more": bool}``,
    trimmed down from WAHA's ``ChatSummary``. If the user has no WhatsApp
    connection there are genuinely no chats to list, so an empty list with
    a 200 is returned. If the session-scoped ``chats/overview`` call fails
    (session-level WAHA/puppeteer error, timeout, etc.) the response
    carries an ``error`` message instead of an empty chat list — the picker
    JS shows it and links the user to ``/connections`` rather than
    silently rendering nothing. This is deliberately *not* pointed at
    ``/dependencies``: that page only probes WAHA's general ``/health``
    endpoint and can be perfectly green while this operator's specific
    session is failing — see ``service_health.py``.
    """
    svc = DeploymentsService(db)
    result = _get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"chats": [], "has_more": False})

    conn = ConnectionsService(db).get_whatsapp(user)
    if not conn or not conn.config.get("waha_session"):
        return JSONResponse({"chats": [], "has_more": False})

    try:
        settings = get_waha_settings().model_copy(update={"session": conn.config["waha_session"]})
        client = WahaClient(settings)
        try:
            # Over-fetch by one so has_more is reliable even when WAHA's
            # merge=true collapses @lid/@c.us pairs below the requested limit.
            raw = await client.get_chats_overview(limit=limit + 1, offset=offset)
        finally:
            await client.close()
    except Exception:
        logger.exception(
            "Chat picker: WAHA chats/overview request failed for dep_id=%s session=%s",
            dep_id,
            conn.config["waha_session"],
        )
        return JSONResponse(
            {
                "chats": [],
                "has_more": False,
                "error": "Could not load chats for this WhatsApp session",
            }
        )

    has_more = len(raw) > limit
    chats = raw[:limit]
    trimmed = [
        {
            "id": c.get("id", ""),
            "name": c.get("name") or "",
            "avatar_initial": _avatar_initial(c.get("name"), c.get("id", "")),
        }
        for c in chats
        if c.get("id")
    ]
    return JSONResponse({"chats": trimmed, "has_more": has_more})


# --- History ---


@router.get("/deployments/{dep_id}/history")
async def deployment_history(
    request: Request,
    dep_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = _get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result

    history_raw = svc.history(dep)
    total = sum(len(msgs) for msgs in history_raw.values())

    # Sort conversations by their latest message timestamp (newest first),
    # falling back to chat_id for legacy timestamp-less buckets so the page
    # is deterministic. Within each bucket messages are reversed so the
    # latest message appears at the top — no scrolling to the bottom.
    def _conv_sort_key(item: tuple[str, list[dict]]) -> tuple[int, str]:
        chat_id, msgs = item
        last_ts = ""
        for m in reversed(msgs):
            ts = m.get("ts")
            if ts:
                last_ts = ts
                break
        # Timestamps sort lexicographically as ISO-8601 UTC; empty (legacy)
        # buckets sort last.
        return (1 if last_ts else 0, last_ts or chat_id)

    history: dict[str, list[dict]] = {}
    for chat_id, msgs in sorted(history_raw.items(), key=_conv_sort_key, reverse=True):
        history[chat_id] = [{**m, "ts": _fmt_ts(m.get("ts"))} for m in reversed(msgs)]

    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "user": user,
            "dep": dep,
            "dep_user": user,
            "history": history,
            "total": total,
        },
    )


# --- Lifecycle ---


@router.post("/deployments/{dep_id}/start")
async def deployment_start(
    request: Request,
    dep_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = _get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result
    try:
        svc.start(dep)
    except ConnectionRequiredError:
        request.session["flash"] = "Connect WhatsApp first before starting."
        return RedirectResponse("/connections", status_code=302)
    except DeploymentStartupError as exc:
        request.session["flash"] = f"Could not start deployment: {exc}"
    return RedirectResponse(f"/deployments/{dep_id}", status_code=302)


@router.post("/deployments/{dep_id}/stop")
async def deployment_stop(
    dep_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = _get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result
    svc.stop(dep)
    return RedirectResponse(f"/deployments/{dep_id}", status_code=302)


@router.post("/deployments/{dep_id}/sleep")
async def deployment_sleep(
    dep_id: int,
    chat_id: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = _get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result
    svc.sleep_chat(dep, chat_id)
    return RedirectResponse(f"/deployments/{dep_id}", status_code=302)


@router.post("/deployments/{dep_id}/wake")
async def deployment_wake(
    dep_id: int,
    chat_id: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = _get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result
    svc.wake_chat(dep, chat_id)
    return RedirectResponse(f"/deployments/{dep_id}", status_code=302)


@router.post("/deployments/{dep_id}/restart")
async def deployment_restart(
    request: Request,
    dep_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = _get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result
    try:
        svc.stop(dep)
        svc.start(dep)
    except (ConnectionRequiredError, DeploymentStartupError) as exc:
        request.session["flash"] = f"restart failed: {exc}"
    except Exception as exc:
        # stop() can raise (e.g. ProcessLookupError from a recycled PID);
        # surface it rather than letting it propagate as an unhandled 500.
        request.session["flash"] = f"restart failed: {exc}"
    return RedirectResponse(f"/deployments/{dep_id}", status_code=302)


@router.post("/deployments/{dep_id}/delete")
async def deployment_delete(
    request: Request,
    dep_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a deployment. WhatsApp connection is left intact."""
    svc = DeploymentsService(db)
    result = _get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result
    svc.delete(dep)
    request.session["flash"] = "Deployment deleted."
    return RedirectResponse("/console", status_code=302)


# --- Settings ---


@router.post("/deployments/{dep_id}/settings")
async def deployment_settings(
    request: Request,
    dep_id: int,
    goal: str = Form(...),
    language: str = Form(...),
    voice: str = Form(""),
    trigger_keyword: str = Form(""),
    timezone: str = Form(""),
    mentions_enabled: str = Form(""),
    whitelist: str = Form(""),
    blacklist: str = Form(""),
    participation_enabled: str = Form(""),
    participation_rate: float = Form(0.15),
    participation_cooldown: int = Form(90),
    participation_streak_max: int = Form(2),
    voice_note_rate: float = Form(0.25),
    voice_note_cooldown: int = Form(300),
    kokoro_voice_map: str = Form(""),
    brain_mandatory: str = Form(""),
    brain_instruction: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = _get_deployment(svc, dep_id, user)
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
    form_fields = dict(await request.form())
    feature_flags = {}
    supported_svcs: list[str] = []
    if bt:
        for flag in bt.feature_flags:
            requested = f"feature_{flag}" in form_fields
            feature_flags[flag] = bool(requested and flag in entitlements)
        supported_svcs = [
            svc for svc in bt.supported_connections if svc not in bt.required_connections
        ]

    from kai.bots.waha.tts import SUPPORTED_KOKORO_LANGS, parse_voice_map

    kokoro_voice_map = kokoro_voice_map.strip()
    unknown_langs = sorted(
        lang for lang in parse_voice_map(kokoro_voice_map) if lang not in SUPPORTED_KOKORO_LANGS
    )
    if unknown_langs:
        request.session["flash"] = (
            f"Unknown Kokoro language code(s) in voice overrides: {', '.join(unknown_langs)}. "
            f"Supported: {', '.join(SUPPORTED_KOKORO_LANGS)}."
        )
        return RedirectResponse(f"/deployments/{dep_id}/settings", status_code=302)

    settings_update = {
        "trigger_keyword": trigger_keyword,
        "timezone": timezone or None,
        "mentions_enabled": mentions_enabled == "true",
        "whitelist": [line.strip() for line in whitelist.splitlines() if line.strip()],
        "blacklist": [line.strip() for line in blacklist.splitlines() if line.strip()],
        "participation": {
            "enabled": participation_enabled == "true",
            "rate": participation_rate,
            "cooldown_seconds": participation_cooldown,
            "streak_max": participation_streak_max,
            "voice_note_rate": voice_note_rate,
            "voice_note_cooldown": voice_note_cooldown,
        },
        # Per-deployment enable of supported connections. A checkbox the UI
        # disabled (connection doesn't exist yet) can still be crafted in a
        # direct POST — that's stored intent, not an executed grant:
        # start() skips injection when the Connection row is absent. The
        # full ``settings`` dict is built here because edit()'s shallow merge
        # (``{**deployment.settings, **value}``) replaces nested keys rather
        # than deep-merging, so partial updates would clobber siblings.
        # Tools that carry an instruction (database) store a nested dict;
        # plain toggles store a bool.
        "tools": _build_tools_update(supported_svcs, form_fields),
        "kokoro_voice_map": kokoro_voice_map,
    }

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
