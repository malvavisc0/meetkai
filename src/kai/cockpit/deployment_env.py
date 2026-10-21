"""Environment construction for bot deployment subprocesses."""

import logging
import os
from collections.abc import Callable

from sqlalchemy.orm import Session

from kai.cockpit import config_writer
from kai.cockpit.bots import WEBHOOK_CONNECTION_TYPES, BotType
from kai.cockpit.deployment_helpers import (
    ConnectionRequiredError,
    DeploymentStartupError,
    tool_enabled,
    tool_instruction,
)
from kai.cockpit.models import Connection, Deployment, User

logger = logging.getLogger(__name__)


SERVICE_ENV_VARS: dict[str, dict] = {
    "database": {
        "fields": {"url": "KAI_SQL_DSN"},
        "instruction": "KAI_SQL_INSTRUCTION",
        "bool_fields": set(),
    },
    "smtp": {
        "fields": {
            "host": "KAI_SMTP_TOOL_HOST",
            "port": "KAI_SMTP_TOOL_PORT",
            "username": "KAI_SMTP_TOOL_USERNAME",
            "password": "KAI_SMTP_TOOL_PASSWORD",
            "from_address": "KAI_SMTP_TOOL_FROM_ADDRESS",
            "use_tls": "KAI_SMTP_TOOL_USE_TLS",
        },
        "instruction": "KAI_SMTP_TOOL_INSTRUCTION",
        "bool_fields": {"use_tls"},
    },
    "calcom": {
        "fields": {"api_key": "KAI_CALCOM_API_KEY", "base_url": "KAI_CALCOM_BASE_URL"},
        "instruction": "KAI_CALCOM_INSTRUCTION",
        "bool_fields": set(),
    },
}


def is_connected(service: str, conn: Connection | None) -> bool:
    """Per-family "is this connection ready?" predicate.

    Ingress-only connections (resend) require the row to exist with all
    declared secret fields non-empty plus ``status == "connected"``.
    """
    if conn is None:
        return False
    if service in WEBHOOK_CONNECTION_TYPES:
        wt = WEBHOOK_CONNECTION_TYPES[service]
        has_all_secrets = all(conn.config.get(f) for f in wt.secret_fields)
        return has_all_secrets and conn.status == "connected"
    return conn.status == "connected"


def inject_connection_env(env: dict[str, str], service: str, conn: Connection) -> bool:
    """Inject env vars for a supported credential connection into ``env``.

    Driven by ``SERVICE_ENV_VARS``. A service not listed raises
    ``NotImplementedError``; a decryption failure becomes
    ``DeploymentStartupError`` so the route surfaces a flash message
    instead of a bare 500.
    """
    if service in WEBHOOK_CONNECTION_TYPES:
        return False
    try:
        svc_vars = SERVICE_ENV_VARS.get(service)
        if svc_vars is None:
            raise NotImplementedError(f"env injection for {service!r} not implemented")
        from kai.cockpit.connections.secrets import decrypt_config

        cfg = decrypt_config(service, conn.config)
        return apply_service_env_vars(env, cfg, svc_vars)
    except DeploymentStartupError:
        raise
    except NotImplementedError:
        raise
    except Exception as exc:
        raise DeploymentStartupError(
            f"could not decrypt {service} connection — reconfigure at /connections/{service}"
        ) from exc


def apply_service_env_vars(env: dict[str, str], cfg: dict, svc_vars: dict) -> bool:
    """Copy a connection's config values into ``env``; return whether any landed."""
    bool_fields = svc_vars.get("bool_fields", set())
    injected = False
    for config_key, env_var in svc_vars.get("fields", {}).items():
        val = cfg.get(config_key)
        if val is None or val == "":
            continue
        if config_key in bool_fields:
            env[env_var] = "true" if val else "false"
        else:
            env[env_var] = str(val)
        injected = True
    return injected


def build_spawn_env(
    deployment: Deployment,
    bt: BotType,
    user: User,
    required_conns: dict[str, Connection],
    brain_conn: Connection | None,
    *,
    db: Session,
    allocate_control_port: Callable[[Session, User], int],
    inject_connection: Callable[[dict[str, str], str, Connection], bool],
    inject_brain: Callable[[dict[str, str], Deployment, Connection], None],
    inject_optional: Callable[[dict[str, str], Deployment, BotType, User], None],
) -> dict[str, str]:
    """Build the spawn environment from connections, Brain, and tools."""
    env: dict[str, str] = {**os.environ}
    # Operator slug for per-user escalation scoping. fail-fast here
    # (before the subprocess spawns) so the error is clear; the DB NOT
    # NULL constraint on users.kai_slug is the persistence guarantee.
    if not user.kai_slug:
        raise DeploymentStartupError(f"cannot spawn bot: user {user.email!r} has no kai_slug")
    env["KAI_OWNER_SLUG"] = user.kai_slug
    # Cockpit URL for bot→cockpit escalation forwarding. Sourced from
    # CockpitSettings.cockpit_internal_url (loopback address bots can
    # reach), not public_url (the browser-facing URL bots can't resolve).
    from kai.cockpit.settings import get_cockpit_settings

    env["KAI_COCKPIT_URL"] = get_cockpit_settings().cockpit_internal_url

    # Required credential connections (e.g. the email bot's smtp).
    # Ingress-only (resend) is a no-op.
    for service, c in required_conns.items():
        try:
            inject_connection(env, service, c)
        except DeploymentStartupError as exc:
            raise ConnectionRequiredError(f"{service} config unreadable: {exc}") from exc

    # Control-port + HMAC-key injection for bot types. The port is stored
    # in Deployment.settings["control_port"] (JSON, no migration) and
    # cleared on stop(). This is the generic path — the next webhook bot
    # needs zero cockpit changes.
    control_port = allocate_control_port(db, user)
    env["KAI_BOT_CONTROL_PORT"] = str(control_port)
    env["KAI_BOT_CONTROL_HOST"] = "0.0.0.0"
    env["KAI_BOT_HMAC_KEY"] = user.hmac_key
    env["KAI_CONFIGS_DIR"] = str(config_writer.CONFIGS_DIR)
    deployment.settings = {**deployment.settings, "control_port": control_port}

    if brain_conn is not None:
        inject_brain(env, deployment, brain_conn)

    inject_optional(env, deployment, bt, user)
    return env


def inject_brain_env(env: dict[str, str], deployment: Deployment, brain_conn: Connection) -> None:
    """Inject Brain workspace, instruction, and mandatory env vars."""
    workspace = brain_conn.config["workspace"]
    if deployment.brain_instruction is not None and deployment.brain_instruction.strip():
        instruction = deployment.brain_instruction
    else:
        instruction = brain_conn.config.get("instruction", "")
    mandatory = deployment.brain_mandatory is True
    env["KAI_BRAIN_WORKSPACE"] = workspace
    env["KAI_BRAIN_INSTRUCTION"] = instruction
    env["KAI_BRAIN_MANDATORY"] = "true" if mandatory else "false"


def inject_optional_tools_env(
    env: dict[str, str],
    deployment: Deployment,
    bt: BotType,
    user: User,
    find_connection: Callable[[User, str], Connection | None],
    inject_connection: Callable[[dict[str, str], str, Connection], bool],
) -> None:
    """Inject env vars for enabled optional tool connections."""
    tools_cfg = deployment.settings.get("tools", {})
    for service in bt.supported_connections:
        if service in bt.required_connections:
            continue
        if not tool_enabled(tools_cfg.get(service, {})):
            continue
        c = find_connection(user, service)
        if c is None:
            continue
        try:
            inject_connection(env, service, c)
        except DeploymentStartupError as exc:
            logger.warning(
                "Skipping %s connection for deployment %s: %s",
                service,
                deployment.id,
                exc,
            )
            continue
        svc_vars = SERVICE_ENV_VARS.get(service, {})
        instr_var = svc_vars.get("instruction")
        if instr_var and any(ev in env for ev in svc_vars.get("fields", {}).values()):
            env[instr_var] = tool_instruction(tools_cfg.get(service, {}))
