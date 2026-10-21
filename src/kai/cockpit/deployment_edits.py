"""Field appliers used when editing a deployment."""

from kai.cockpit.bots import BotType
from kai.cockpit.deployment_helpers import require_non_empty, require_supported_language
from kai.cockpit.models import Deployment


def apply_goal(deployment: Deployment, bt: BotType, value: object) -> bool:
    """Set the deployment goal."""
    require_non_empty(value, "goal")
    deployment.goal = str(value).strip()
    return False


def apply_language(deployment: Deployment, bt: BotType, value: object) -> bool:
    """Set the deployment language and mirror it into settings."""
    require_non_empty(value, "language")
    language = str(value).strip()
    require_supported_language(language)
    deployment.language = language
    deployment.settings["language"] = deployment.language
    return True


def apply_feature_flags(deployment: Deployment, bt: BotType, value: object) -> bool:
    """Set feature flags after rejecting names the bot type doesn't declare."""
    if not isinstance(value, dict):
        raise ValueError("feature_flags must be a dict")
    invalid = set(value.keys()) - set(bt.feature_flags)
    if invalid:
        raise ValueError(f"invalid feature flags: {invalid}")
    deployment.feature_flags = value
    return False


def apply_settings(deployment: Deployment, bt: BotType, value: object) -> bool:
    """Merge settings after rejecting empty required values."""
    if not isinstance(value, dict):
        raise ValueError("settings must be a dict")
    for req in bt.required_settings:
        if req in value and not value[req]:
            raise ValueError(f"setting '{req}' cannot be empty")
    deployment.settings = {**deployment.settings, **value}
    return True


def apply_brain_mandatory(deployment: Deployment, bt: BotType, value: object) -> bool:
    """Set whether the Brain is mandatory for this deployment."""
    if value is not None and not isinstance(value, bool):
        raise ValueError("brain_mandatory must be a bool or null")
    deployment.brain_mandatory = value
    return False


def apply_brain_instruction(deployment: Deployment, bt: BotType, value: object) -> bool:
    """Set the per-deployment Brain instruction."""
    if value is not None and not isinstance(value, str):
        raise ValueError("brain_instruction must be a string or null")
    deployment.brain_instruction = str(value).strip() if value is not None else None
    return False


def apply_template(deployment: Deployment, bt: BotType, value: object) -> bool:
    """Set the deployment's template name."""
    deployment.template = str(value)
    return False


def apply_tool_overrides(deployment: Deployment, bt: BotType, value: object) -> bool:
    """Set template tool enable/disable overrides."""
    if not isinstance(value, dict):
        raise ValueError("tool_overrides must be a dict")
    if set(value.keys()) - {"enable", "disable"}:
        raise ValueError("tool_overrides must only have 'enable' and 'disable' keys")
    deployment.tool_overrides = {
        "enable": list(value.get("enable", [])),
        "disable": list(value.get("disable", [])),
    }
    return False
