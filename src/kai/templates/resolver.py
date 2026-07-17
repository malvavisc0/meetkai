from __future__ import annotations

import logging
from dataclasses import dataclass, field

from kai.agent.tools import get_tools
from kai.templates.schema import TemplateDef

logger = logging.getLogger(__name__)

_DEFAULT_TOOL_NAMES: frozenset[str] = frozenset(
    tool.metadata.name for tool in get_tools() if tool.metadata.name
)

# Env vars each tool needs to be configured. A tool is "configured" only when
# ALL listed vars are set — mirroring the *_enabled gates on each tool's
# settings (e.g. BrainSettings.brain_enabled needs base_url AND lightrag_api_key;
# SmtpSettings.smtp_enabled needs host AND username AND password AND from_address).
_TOOL_ENV_MAP: dict[str, list[str]] = {
    "brain_query": ["KAI_BRAIN_BASE_URL", "KAI_BRAIN_LIGHTRAG_API_KEY"],
    "sql_query": ["KAI_SQL_DSN"],
    "send_email": [
        "KAI_SMTP_TOOL_HOST",
        "KAI_SMTP_TOOL_USERNAME",
        "KAI_SMTP_TOOL_PASSWORD",
        "KAI_SMTP_TOOL_FROM_ADDRESS",
    ],
    "calcom": ["KAI_CALCOM_API_KEY"],
}

_WAHA_VALID_ACTIONS = {
    "reply",
    "send_voice_note",
    "silent",
    "sleep",
    "send_dm",
    "send_to_group",
    "console",
}

_EMAIL_VALID_ACTIONS = {
    "reply",
    "silent",
    "console",
}

_VALID_ACTIONS_BY_TRANSPORT = {
    "waha": _WAHA_VALID_ACTIONS,
    "email": _EMAIL_VALID_ACTIONS,
}


@dataclass(frozen=True)
class ToolResolution:
    final_tools: frozenset[str]
    missing_required: list[str] = field(default_factory=list)
    rejected_disable: list[str] = field(default_factory=list)


def resolve_config(
    template: TemplateDef,
    config_file_data: dict | None,
    cli_overrides: dict,
    config_cls: type,
) -> object:
    defaults = config_cls().model_dump()
    merged = _deep_merge(defaults, template.config)
    if config_file_data:
        merged = _deep_merge(merged, config_file_data)
    merged = _deep_merge(merged, cli_overrides)
    return config_cls.model_validate(merged)


def resolve_tools(
    template: TemplateDef,
    operator_enable: list[str],
    operator_disable: list[str],
) -> ToolResolution:
    default_tools = frozenset(_DEFAULT_TOOL_NAMES)
    template_required = frozenset(template.tools.required)

    cannot_disable = set(default_tools) | template_required

    rejected = []
    for tool in operator_disable:
        if tool in cannot_disable:
            reason = "default" if tool in default_tools else "required by template"
            rejected.append(f"{tool} ({reason} — cannot be disabled)")

    tools: set[str] = set(default_tools)
    tools |= template_required
    for tool in template.tools.optional:
        if _is_tool_configured(tool):
            tools.add(tool)
    for tool in operator_enable:
        tools.add(tool)
    for tool in operator_disable:
        if tool not in cannot_disable and tool in tools:
            tools.discard(tool)

    missing = []
    for tool in template_required:
        if not _is_tool_configured(tool):
            env = _TOOL_ENV_MAP.get(tool, ["unknown"])
            missing.append(f"{tool} (requires {', '.join(env)})")

    return ToolResolution(
        final_tools=frozenset(tools),
        missing_required=missing,
        rejected_disable=rejected,
    )


def validate_tools(template: TemplateDef) -> list[str]:
    """Return human-readable errors for any `required` tool whose env vars
    are not configured. Empty list = all required tools are available.

    Reads env vars directly (the same source the tool registrations use); a
    `settings` object would just re-export them, so it is not taken here.
    """
    missing = []
    for tool in template.tools.required:
        if not _is_tool_configured(tool):
            env = _TOOL_ENV_MAP.get(tool, ["unknown"])
            missing.append(f"{tool} (requires {', '.join(env)})")
    return missing


def validate_actions(template: TemplateDef) -> list[str]:
    valid = _VALID_ACTIONS_BY_TRANSPORT.get(template.transport, set())
    errors = []
    for action in template.actions:
        if action not in valid:
            errors.append(f"action {action!r} is not valid for transport {template.transport!r}")
    return errors


def _is_tool_configured(tool_name: str) -> bool:
    env_vars = _TOOL_ENV_MAP.get(tool_name)
    if not env_vars:
        return True
    import os

    return all(os.environ.get(v) for v in env_vars)


def _deep_merge(a: dict, b: dict) -> dict:
    result = a.copy()
    for key, value in b.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
