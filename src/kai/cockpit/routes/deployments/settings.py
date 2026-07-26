"""Deployment settings: ``GET``/``POST /deployments/{dep_id}/settings``."""

from collections.abc import Callable

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from kai.agent.tools.email import DEFAULT_DISPLAY_NAME
from kai.cockpit.app import templates
from kai.cockpit.auth import require_user
from kai.cockpit.bots import (
    ALL_LANGUAGES,
    BOT_TYPES,
    CAPABILITY_LABELS,
    CREDENTIAL_TYPES,
)
from kai.cockpit.brains import BrainsService
from kai.cockpit.connections.service import ConnectionsService
from kai.cockpit.db import get_db
from kai.cockpit.deployments import DeploymentsService, _tool_enabled, _tool_instruction
from kai.cockpit.flash import flash
from kai.cockpit.models import User
from kai.cockpit.routes.deployments._shared import (
    SETTINGS_TEMPLATES,
    TOOLS_WITH_INSTRUCTION,
    build_tools_update,
    get_deployment,
)
from kai.templates import TemplateRegistry
from kai.templates.resolver import (
    KNOWN_TOOL_NAMES,
    resolve_tools,
    tool_configured_map,
    validate_tools,
)

router = APIRouter()

# A per-bot-type settings parser takes the deployment id, request (for
# flash-message redirects on validation errors), and the text-only form
# fields, and returns either:
#   - a RedirectResponse (a validation error the operator must fix), or
#   - a dict of settings updates to merge into ``settings_update``.
SettingsParseResult = dict | RedirectResponse
SettingsParser = Callable[[int, Request, dict], SettingsParseResult]


def _parse_email_settings(dep_id: int, request: Request, form_fields: dict) -> SettingsParseResult:
    """Email-only settings: a blocklist of sender addresses to silently ignore."""
    updates = {
        "blacklist": [
            line.strip().lower()
            for line in (form_fields.get("blacklist", "") or "").splitlines()
            if line.strip()
        ],
        "display_name": (form_fields.get("display_name", "") or "").strip() or DEFAULT_DISPLAY_NAME,
    }
    return updates


# Bot types with no entry here (e.g. a brand-new bot type) simply get no
# extra settings parsed — ``settings_update`` keeps the shared
# timezone/tools keys only, same as before this table existed.
_SETTINGS_PARSERS: dict[str, SettingsParser] = {
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
    entitlements = {k for k, v in (user.feature_flags or {}).items() if v}
    feature_flags = _build_feature_flags(bt, entitlements)
    available_conns = {c.service for c in ConnectionsService(db).list_for_user(user)}
    supported_tools = _build_supported_tools(bt, available_conns)
    tools_state = _build_tools_state(supported_tools, dep.settings.get("tools", {}))
    tmpl = _resolve_template(dep)
    template_tools, template_warnings = _build_template_tools(tmpl, dep.tool_overrides or {})

    flash = request.session.pop("flash", None)

    brain = BrainsService(db).get_brain(user)

    template_name = SETTINGS_TEMPLATES.get(dep.bot_type, SETTINGS_TEMPLATES["default"])

    return templates.TemplateResponse(
        request,
        template_name,
        {
            "user": user,
            "dep": dep,
            "dep_user": user,
            "languages": ALL_LANGUAGES,
            "feature_flags": feature_flags,
            "capability_labels": CAPABILITY_LABELS,
            "has_brain": brain is not None,
            "supported_tools": supported_tools,
            "tools_state": tools_state,
            "tools_with_instruction": TOOLS_WITH_INSTRUCTION,
            "template_tools": template_tools,
            "template": tmpl,
            "template_warnings": template_warnings,
            "flash": flash,
            "default_display_name": DEFAULT_DISPLAY_NAME,
        },
    )


def _build_feature_flags(bt, entitlements: set[str]) -> list[tuple[str, str, bool]]:
    """Render every flag the bot type declares: an unentitled flag shows up
    disabled + unchecked so the operator sees what's possible. The POST
    handler clamps submitted values to entitlements so a crafted checkbox
    can't self-enable anything."""
    flags: list[tuple[str, str, bool]] = []
    if not bt:
        return flags
    for flag in bt.feature_flags:
        label = CAPABILITY_LABELS.get(flag) or flag.capitalize()
        flags.append((flag, label, flag in entitlements))
    return flags


def _build_supported_tools(bt, available_conns: set[str]) -> list[tuple[str, str, bool]]:
    """One checkbox per supported connection that isn't required. Each is
    disabled when the connection doesn't exist yet."""
    tools: list[tuple[str, str, bool]] = []
    if not bt:
        return tools
    for conn_svc in bt.supported_connections:
        if conn_svc in bt.required_connections:
            continue
        label = (
            CREDENTIAL_TYPES[conn_svc].label
            if conn_svc in CREDENTIAL_TYPES
            else conn_svc.capitalize()
        )
        tools.append((conn_svc, label, conn_svc in available_conns))
    return tools


def _build_tools_state(
    supported_tools: list[tuple[str, str, bool]], tools_enabled: dict
) -> dict[str, dict]:
    state: dict[str, dict] = {}
    for conn_svc, _, available in supported_tools:
        raw = tools_enabled.get(conn_svc, {})
        state[conn_svc] = {
            "enabled": _tool_enabled(raw),
            "instruction": _tool_instruction(raw),
            "available": available,
        }
    return state


def _resolve_template(dep):
    try:
        return TemplateRegistry.bundled().get(dep.bot_type, dep.template)
    except FileNotFoundError:
        return None


def _build_template_tools(tmpl, saved_overrides: dict) -> tuple[list[tuple[str, bool]], list[str]]:
    """Template optional tool toggles: each row is a checkbox the operator
    can uncheck to disable that optional tool for the deployment."""
    if tmpl is None:
        return [], []
    configured = tool_configured_map(tmpl)
    tools: list[tuple[str, bool]] = []
    for tool_name in sorted(set(tmpl.tools.optional) & KNOWN_TOOL_NAMES):
        is_on = _template_tool_is_on(tool_name, configured.get(tool_name, True), saved_overrides)
        tools.append((tool_name, is_on))
    return tools, validate_tools(tmpl)


def _template_tool_is_on(tool_name: str, is_configured: bool, saved: dict) -> bool:
    # Reflect persisted overrides: explicitly-disabled stays unchecked;
    # explicitly-enabled stays checked; otherwise default to "on" for
    # configured optional tools.
    saved_disable = set(saved.get("disable", []))
    saved_enable = set(saved.get("enable", []))
    if tool_name in saved_disable:
        return False
    if tool_name in saved_enable:
        return True
    return is_configured


def _build_feature_flags_for_post(bt, entitlements: set[str], form_fields: dict) -> dict:
    # Clamp submitted feature flag values to the user's entitlements — the
    # POST can spoof any checkbox name, so we silently drop unentitled
    # flags rather than returning 403 (avoids leaking entitlement state).
    flags: dict = {}
    if not bt:
        return flags
    for flag in bt.feature_flags:
        requested = f"feature_{flag}" in form_fields
        flags[flag] = bool(requested and flag in entitlements)
    return flags


def _supported_optional_services(bt) -> list[str]:
    if not bt:
        return []
    return [
        conn_svc for conn_svc in bt.supported_connections if conn_svc not in bt.required_connections
    ]


def _build_tool_overrides(tmpl, form_fields: dict) -> dict:
    # Parse template tool overrides from the form. Unchecked optional tools
    # go into the "disable" list; checked ones stay on (the resolver still
    # env-gates any optional tool, so enabling an unconfigured tool is a
    # no-op rather than a crash).
    overrides: dict = {"enable": [], "disable": []}
    if tmpl is None:
        return overrides
    for tool_name in sorted(tmpl.tools.optional):
        if f"tool_override_{tool_name}" not in form_fields:
            overrides["disable"].append(tool_name)
    return overrides


def _validate_tool_overrides(
    request: Request, dep_id: int, tmpl, tool_overrides: dict
) -> RedirectResponse | None:
    # Server-side validation: reject disabling default/required tools and
    # unknown tool names. Uses resolve_tools() so the resolver rules are the
    # single source of truth — the cockpit never reimplements them.
    if tmpl is None:
        return None
    resolution = resolve_tools(tmpl, tool_overrides["enable"], tool_overrides["disable"])
    if resolution.rejected_disable:
        flash(request, "warn", f"Cannot disable: {resolution.rejected_disable[0]}")
        return RedirectResponse(f"/deployments/{dep_id}/settings", status_code=302)
    if resolution.rejected_unknown:
        flash(request, "warn", f"Unknown tool: {resolution.rejected_unknown[0]}")
        return RedirectResponse(f"/deployments/{dep_id}/settings", status_code=302)
    return None


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

    form_fields = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    bt = BOT_TYPES.get(dep.bot_type)
    entitlements = {k for k, v in (user.feature_flags or {}).items() if v}
    tmpl = _resolve_template(dep)
    tool_overrides = _build_tool_overrides(tmpl, form_fields)

    redirect = _validate_tool_overrides(request, dep_id, tmpl, tool_overrides)
    if redirect is not None:
        return redirect

    parsed = _parse_bot_settings(dep, dep_id, request, form_fields)
    if isinstance(parsed, RedirectResponse):
        return parsed

    edit_fields = _build_edit_fields(
        bt,
        dep,
        goal,
        language,
        timezone,
        brain_mandatory,
        brain_instruction,
        form_fields,
        entitlements,
        tool_overrides,
        parsed,
    )
    redirect = _apply_edit(request, dep_id, svc, dep, edit_fields)
    if redirect is not None:
        return redirect

    _flash_saved(request, dep)
    return RedirectResponse(f"/deployments/{dep_id}/settings", status_code=302)


def _build_edit_fields(
    bt,
    dep,
    goal,
    language,
    timezone,
    brain_mandatory,
    brain_instruction,
    form_fields,
    entitlements,
    tool_overrides,
    parsed,
) -> dict:
    feature_flags = _build_feature_flags_for_post(bt, entitlements, form_fields)
    supported_svcs = _supported_optional_services(bt)
    settings_update: dict = {
        "timezone": timezone or None,
        "tools": build_tools_update(supported_svcs, form_fields),
    }
    if isinstance(parsed, dict):
        settings_update.update(parsed)
    return {
        "goal": goal,
        "language": language,
        "feature_flags": feature_flags,
        "settings": settings_update,
        "brain_mandatory": brain_mandatory == "true",
        "brain_instruction": brain_instruction.strip() or None,
        "tool_overrides": tool_overrides,
    }


def _apply_edit(
    request: Request,
    dep_id: int,
    svc: DeploymentsService,
    dep,
    edit_fields: dict,
) -> RedirectResponse | None:
    try:
        svc.edit(dep, **edit_fields)
    except ValueError as exc:
        flash(request, "warn", str(exc))
        return RedirectResponse(f"/deployments/{dep_id}/settings", status_code=302)
    return None


def _flash_saved(request: Request, dep) -> None:
    if dep.status == "running":
        flash(request, "info", "Settings saved. Restart to apply.")
    else:
        flash(request, "success", "Settings saved.")


def _parse_bot_settings(dep, dep_id: int, request: Request, form_fields: dict):
    parser = _SETTINGS_PARSERS.get(dep.bot_type)
    if parser is None:
        return {}
    return parser(dep_id, request, form_fields)
