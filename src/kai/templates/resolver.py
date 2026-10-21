import logging
from dataclasses import dataclass, field

from pydantic import BaseModel

from kai.agent.tools import get_tools
from kai.agent.tools.escalate import EscalationState
from kai.templates.schema import TemplateDef

logger = logging.getLogger(__name__)

DEFAULT_TOOL_NAMES: frozenset[str] = frozenset(
    tool.metadata.name for tool in get_tools(EscalationState()) if tool.metadata.name
)

# Non-disableable defaults: safety tools that must stay available on every
# deployment regardless of template or operator preference.
NON_DISABLEABLE_DEFAULTS: frozenset[str] = frozenset({"escalate", "blacklist", "calculate"})

TOOL_ENV_MAP: dict[str, list[str]] = {
    "brain_query": ["KAI_BRAIN_BASE_URL", "KAI_BRAIN_MORPHIK_TOKEN"],
    "sql_query": ["KAI_SQL_DSN"],
    "send_email": [
        "KAI_SMTP_TOOL_HOST",
        "KAI_SMTP_TOOL_USERNAME",
        "KAI_SMTP_TOOL_PASSWORD",
        "KAI_SMTP_TOOL_FROM_ADDRESS",
    ],
    "calcom": ["KAI_CALCOM_API_KEY"],
}

EMAIL_VALID_ACTIONS = {
    "reply",
    "silent",
    "console",
}

# Every tool name the system can ever register. Used to reject phantom
# ``--enable-tools`` typos (e.g. ``barin_query``) at boot — see resolve_tools.
KNOWN_TOOL_NAMES: frozenset[str] = frozenset(
    [
        "web_search",
        "get_webpage_content",
        "get_time_in_timezone",
        "get_weather",
        "calculate",
        "schedule_task",
        "list_tasks",
        "cancel_task",
        "record_note",
        "get_conversation_messages",
        "brain_query",
        "sql_query",
        "describe_database",
        "send_email",
        "calcom",
        "list_event_types",
        "find_available_slots",
        "book_appointment",
        "reschedule_booking",
        "cancel_booking",
        "escalate",
        "blacklist",
    ]
)

VALID_ACTIONS_BY_TRANSPORT = {
    "email": EMAIL_VALID_ACTIONS,
}


@dataclass(frozen=True)
class ToolResolution:
    """Resolved tools and workflow prompts for a deployment."""

    final_tools: frozenset[str]
    missing_required: list[str] = field(default_factory=list)
    rejected_disable: list[str] = field(default_factory=list)
    rejected_unknown: list[str] = field(default_factory=list)


def resolve_config[T: BaseModel](
    template: TemplateDef,
    config_file_data: dict | None,
    cli_overrides: dict,
    config_cls: type[T],
) -> T:
    """Layer defaults, template, config file, and CLI overrides into a config."""
    defaults = config_cls().model_dump()
    merged = deep_merge(defaults, template.config)
    if config_file_data:
        merged = deep_merge(merged, config_file_data)
    merged = deep_merge(merged, cli_overrides)
    return config_cls.model_validate(merged)


def resolve_tools(
    template: TemplateDef,
    operator_enable: list[str],
    operator_disable: list[str],
) -> ToolResolution:
    """Resolve the final tool set plus rejected and missing-tool diagnostics."""
    template_required = frozenset(template.tools.required)
    cannot_disable = NON_DISABLEABLE_DEFAULTS | template_required

    rejected_disable = build_rejected_disable(operator_disable, cannot_disable, template_required)
    rejected_unknown = [t for t in operator_enable if t not in KNOWN_TOOL_NAMES]
    tools = build_tool_set(template, operator_enable, operator_disable, cannot_disable)
    missing = build_missing_required(template_required)

    return ToolResolution(
        final_tools=frozenset(tools),
        missing_required=missing,
        rejected_disable=rejected_disable,
        rejected_unknown=rejected_unknown,
    )


def build_rejected_disable(
    operator_disable: list[str], cannot_disable: frozenset[str], template_required: frozenset[str]
) -> list[str]:
    """List disable requests refused because the tool is required or protected."""
    rejected = []
    for tool in operator_disable:
        if tool not in cannot_disable:
            continue
        reason = (
            "required by template"
            if tool in template_required
            else "safety tool — cannot be disabled"
        )
        rejected.append(f"{tool} ({reason} — cannot be disabled)")
    return rejected


def build_tool_set(
    template: TemplateDef,
    operator_enable: list[str],
    operator_disable: list[str],
    cannot_disable: frozenset[str],
) -> set[str]:
    """Compute the enabled tools from defaults, template, config, and overrides."""
    tools: set[str] = set(frozenset(DEFAULT_TOOL_NAMES))
    tools |= frozenset(template.tools.required)
    tools |= frozenset(template.tools.bot_tools)
    for tool in template.tools.optional:
        if is_tool_configured(tool):
            tools.add(tool)
    for tool in operator_enable:
        if tool in KNOWN_TOOL_NAMES:
            tools.add(tool)
    for tool in operator_disable:
        if tool not in cannot_disable and tool in tools:
            tools.discard(tool)
    return tools


def build_missing_required(template_required: frozenset[str]) -> list[str]:
    """List required tools whose environment variables are not configured."""
    missing = []
    for tool in template_required:
        if not is_tool_configured(tool):
            env = TOOL_ENV_MAP.get(tool, ["unknown"])
            missing.append(f"{tool} (requires {', '.join(env)})")
    return missing


def validate_tools(template: TemplateDef) -> list[str]:
    """Return human-readable errors for any `required` tool whose env vars
    are not configured. Empty list = all required tools are available.

    Reads env vars directly (the same source the tool registrations use).
    """
    missing = []
    for tool in template.tools.required:
        if not is_tool_configured(tool):
            env = TOOL_ENV_MAP.get(tool, ["unknown"])
            missing.append(f"{tool} (requires {', '.join(env)})")
    return missing


def validate_actions(template: TemplateDef) -> list[str]:
    """Return errors for actions the template's transport does not support."""
    valid = VALID_ACTIONS_BY_TRANSPORT.get(template.transport, set())
    return [
        f"action {action!r} is not valid for transport {template.transport!r}"
        for action in template.actions
        if action not in valid
    ]


def tool_configured_map(template: TemplateDef) -> dict[str, bool]:
    """Map every tool declared by ``template`` to whether its env is
    currently configured. Used by the cockpit preview/warnings."""
    return {t: is_tool_configured(t) for t in {*template.tools.required, *template.tools.optional}}


def is_tool_configured(tool_name: str) -> bool:
    """Report whether every environment variable a tool requires is set."""
    env_vars = TOOL_ENV_MAP.get(tool_name)
    if not env_vars:
        return True
    import os

    return all(os.environ.get(v) for v in env_vars)


def deep_merge(a: dict, b: dict) -> dict:
    """Recursively merge ``b`` into ``a``, returning a new dict."""
    result = a.copy()
    for key, value in b.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result
