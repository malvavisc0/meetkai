"""Deployments service: shared code path for CLI and web.

Authorization scope differs (CLI: admin/any user; web: self only), but both
call the same methods.
"""

import importlib
import logging
import os
import subprocess
import threading
from collections.abc import Callable

from sqlalchemy.orm import Session

from kai.cockpit import (
    config_writer,
    deployment_chat,
    deployment_edits,
    deployment_env,
    deployment_process,
)
from kai.cockpit.bots import BOT_TYPES, BotType
from kai.cockpit.deployment_env import inject_connection_env, is_connected
from kai.cockpit.deployment_helpers import (
    ConnectionRequiredError,
    DeploymentStartupError,
    attention_reason,
    instance_id_for,
    kai_argv_prefix,
    tool_enabled,
    tool_instruction,
    topbar_status,
    validate_create_inputs,
)
from kai.cockpit.models import Connection, Deployment, User
from kai.runs import RunRecord, RunRegistry, runs_path
from kai.utils.common import now_iso

logger = logging.getLogger(__name__)

__all__ = [
    "ConnectionRequiredError",
    "DeploymentStartupError",
    "DeploymentsService",
    "attention_reason",
    "inject_connection_env",
    "instance_id_for",
    "is_connected",
    "kai_argv_prefix",
    "reconcile_deployments",
    "tool_enabled",
    "tool_instruction",
    "topbar_status",
    "validate_create_inputs",
]


class DeploymentsService:
    """Create, inspect, and control bot deployments."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self._run_cache: dict[tuple[int, str | None], RunRecord | None] = {}

    def user_for(self, deployment: Deployment) -> User:
        """Return the deployment's owning Operator."""
        user = self.db.get(User, deployment.user_id)
        if user is None:
            raise ValueError(f"deployment {deployment.id} has no Operator")
        return user

    def deployment_instance_id(self, deployment: Deployment, *, user: User | None = None) -> str:
        """Return the run-registry instance id for a deployment."""
        if user is None:
            user = self.user_for(deployment)
        return instance_id_for(deployment.bot_type, user.email)

    def allocate_control_port(self, db: Session) -> int:
        """Pick a free control port from 8200-8299."""
        used: set[int] = set()
        for dep in db.query(Deployment).filter(Deployment.status == "running").all():
            cp = dep.settings.get("control_port")
            if isinstance(cp, int):
                used.add(cp)
        for port in range(8200, 8300):
            if port not in used:
                return port
        raise RuntimeError(f"no available control ports in range 8200-8299 ({len(used)} in use)")

    def run_registry(self, deployment: Deployment, *, user: User | None = None) -> RunRegistry:
        """Open the run registry for a deployment."""
        from kai.config.settings import get_settings

        settings = get_settings()
        instance_id = self.deployment_instance_id(deployment, user=user)
        return RunRegistry(runs_path(settings.agent_history_folder, instance_id))

    def resolve_run(self, deployment: Deployment) -> RunRecord | None:
        """Return the deployment's live RunRecord, or None when there is none.

        ``RunRegistry.get`` only hands out live records (it prunes stale and
        PID-reused ones), so every caller — status probes, event forwarding,
        the duplicate-start guard — shares one liveness regime with the CLI.
        None also covers a live record whose ``run_id`` differs (a manually
        started bot on this instance): the registry entry is not ours to act
        on.
        """
        if not deployment.run_id:
            return None
        cache_key = (deployment.id, deployment.run_id)
        if cache_key in self._run_cache:
            return self._run_cache[cache_key]
        record = self.run_registry(deployment).get()
        if record is None or record.run_id != deployment.run_id:
            self._run_cache[cache_key] = None
            return None
        self._run_cache[cache_key] = record
        return record

    def sign_body(self, record: RunRecord, body: bytes) -> str:
        """Compute the HMAC signature for a request body sent to the bot."""
        return deployment_chat.sign_body(record, body)

    def call_bot(
        self,
        record: RunRecord,
        method: str,
        path: str,
        body: bytes = b"",
        *,
        timeout: float = 30.0,
    ) -> dict:
        """Make an HMAC-signed HTTP call to the running bot.

        Returns the JSON response dict on success, or an error dict on failure.
        """
        return deployment_chat.call_bot(
            record, method, path, body, timeout=timeout, sign=self.sign_body
        )

    def forward_event(self, deployment: Deployment, path: str, body: bytes) -> bool:
        """Forward a normalized inbound event to a running deployment's bot.

        Returns True if the bot accepted the event, False if the bot isn't
        reachable or rejected it. ``call_bot`` returns ``{"ok": False, ...}``
        on every failure shape, so ``False`` is the one failure signal.
        """
        return deployment_chat.forward_event(self, deployment, path, body)

    def get(self, deployment_id: int) -> Deployment | None:
        """Return a deployment by primary key, or None when it does not exist."""
        return self.db.query(Deployment).filter(Deployment.id == deployment_id).first()

    def list_for_user(self, user_id: int) -> list[Deployment]:
        """Return every deployment owned by the given operator."""
        return self.db.query(Deployment).filter(Deployment.user_id == user_id).all()

    def stop_all_for_user(self, user_id: int) -> int:
        """Stop every deployment of a user; return how many were stopped.

        Used when an operator is disabled: bot subprocesses run independently
        of the web session gate, so disabling must also stop their bots.
        A failure to stop one deployment is logged and skipped.
        """
        stopped = 0
        for dep in self.list_for_user(user_id):
            if dep.status != "running" and not dep.run_id:
                continue
            try:
                self.stop(dep)
                stopped += 1
            except Exception:
                logger.exception("could not stop deployment %s", dep.id)
        return stopped

    def get_for_user_and_type(self, user_id: int, bot_type: str) -> Deployment | None:
        """Return the operator's deployment for one bot type, if any."""
        return (
            self.db.query(Deployment)
            .filter(Deployment.user_id == user_id, Deployment.bot_type == bot_type)
            .first()
        )

    def create(
        self,
        user: User,
        bot_type: str,
        goal: str,
        language: str,
        template: str = "general",
    ) -> Deployment:
        """Create a deployment. Validates required fields."""
        validate_create_inputs(user, bot_type, goal, language)
        bt = BOT_TYPES[bot_type]
        self.require_connections(bt, user)
        self.reject_duplicate(bot_type, user)

        # Seed settings from the bot type's own BotConfig so each transport
        # carries only its own schema. Bots follow the
        # ``kai.bots.{bot_type}.setup.BotConfig`` convention.
        setup = importlib.import_module(f"kai.bots.{bot_type}.setup")
        default_config = setup.BotConfig(language=language, timezone=user.timezone)
        settings = default_config.model_dump()

        feature_flags = {f: False for f in bt.feature_flags}

        dep = Deployment(
            user_id=user.id,
            bot_type=bot_type,
            goal=goal.strip(),
            language=language.strip(),
            template=template,
            settings=settings,
            feature_flags=feature_flags,
            status="stopped",
            desired_state="stopped",
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        self.db.add(dep)
        self.db.commit()
        self.db.refresh(dep)
        return dep

    def require_connections(self, bt: BotType, user: User) -> None:
        """Raise when a required connection is missing or not ready."""
        for service in bt.required_connections:
            c = self.find_connection(user, service)
            if not is_connected(service, c):
                raise ConnectionRequiredError(f"Connect {service} first at /connections")

    def reject_duplicate(self, bot_type: str, user: User) -> None:
        """Raise when the operator already has a deployment of this bot type."""
        existing = (
            self.db.query(Deployment)
            .filter(Deployment.user_id == user.id, Deployment.bot_type == bot_type)
            .first()
        )
        if existing:
            raise ValueError(
                f"Operator '{user.email}' already has a {bot_type} deployment (id={existing.id})"
            )

    def edit(self, deployment: Deployment, **fields: object) -> Deployment:
        """Apply field edits, persist config, and flag a restart when running."""
        bt = BOT_TYPES.get(deployment.bot_type)
        if bt is None:
            raise ValueError(f"unknown bot type: {deployment.bot_type}")

        settings_changed = False
        apply = FIELD_APPLIERS
        for key, value in fields.items():
            handler = apply.get(key)
            if handler is None:
                continue
            settings_changed = handler(self, deployment, bt, value) or settings_changed

        if "language" in fields:
            deployment.settings["language"] = deployment.language
        elif settings_changed:
            deployment.language = deployment.settings.get("language", deployment.language)

        if deployment.status == "running":
            deployment.needs_restart = True

        deployment.updated_at = now_iso()

        # write_config may normalize ``deployment.settings``, so run it before
        # the commit to persist any such mutation alongside the edit.
        try:
            config_writer.write_config(deployment, self.deployment_instance_id(deployment))
        except OSError:
            logger.warning("Failed to write config for deployment %s", deployment.id, exc_info=True)

        self.db.commit()
        return deployment

    def apply_goal(self, deployment: Deployment, bt: BotType, value: object) -> bool:
        """Set the deployment goal."""
        return deployment_edits.apply_goal(deployment, bt, value)

    def apply_language(self, deployment: Deployment, bt: BotType, value: object) -> bool:
        """Set the deployment language and mirror it into settings."""
        return deployment_edits.apply_language(deployment, bt, value)

    def apply_feature_flags(self, deployment: Deployment, bt: BotType, value: object) -> bool:
        """Set feature flags after rejecting names the bot type doesn't declare."""
        return deployment_edits.apply_feature_flags(deployment, bt, value)

    def apply_settings(self, deployment: Deployment, bt: BotType, value: object) -> bool:
        """Merge settings after rejecting empty required values."""
        return deployment_edits.apply_settings(deployment, bt, value)

    def apply_brain_mandatory(self, deployment: Deployment, bt: BotType, value: object) -> bool:
        """Set whether the Brain is mandatory for this deployment."""
        return deployment_edits.apply_brain_mandatory(deployment, bt, value)

    def apply_brain_instruction(self, deployment: Deployment, bt: BotType, value: object) -> bool:
        """Set the per-deployment Brain instruction."""
        return deployment_edits.apply_brain_instruction(deployment, bt, value)

    def apply_template(self, deployment: Deployment, bt: BotType, value: object) -> bool:
        """Set the deployment's template name."""
        return deployment_edits.apply_template(deployment, bt, value)

    def apply_tool_overrides(self, deployment: Deployment, bt: BotType, value: object) -> bool:
        """Set template tool enable/disable overrides."""
        return deployment_edits.apply_tool_overrides(deployment, bt, value)

    def start(self, deployment: Deployment) -> None:
        """Start a deployment: check connection, write config, spawn subprocess."""
        self._run_cache.clear()
        bt = BOT_TYPES.get(deployment.bot_type)
        if bt is None:
            raise ValueError(f"unknown bot type: {deployment.bot_type}")

        user = self.user_for(deployment)
        instance_id = self.deployment_instance_id(deployment, user=user)

        # Refuse to spawn a second bot on top of a live one: a duplicate would
        # handle the same inbox and orphan the first process (its run record
        # gets overwritten). Reconcile only calls start() for dead bots, so
        # this guard is safe there too. ``resolve_run`` returns live records
        # only, so non-None means a bot is up.
        if self.resolve_run(deployment) is not None:
            raise DeploymentStartupError(f"deployment {deployment.id} is already running")

        required_conns = self.resolve_required_connections(bt, user)
        brain_conn = self.find_connection(user, "morphik")

        config_writer.write_config(deployment, instance_id)

        argv = self.build_spawn_argv(deployment, user)
        env = self.build_spawn_env(deployment, bt, user, required_conns, brain_conn)

        run_id = self.spawn_and_await_run_id(deployment, user, argv, env)

        deployment.run_id = run_id
        deployment.status = "running"
        deployment.desired_state = "running"
        deployment.needs_restart = False
        deployment.updated_at = now_iso()
        self.db.commit()

    def resolve_required_connections(self, bt: BotType, user: User) -> dict[str, Connection]:
        """Return the operator's ready connections required by the bot type."""
        required: dict[str, Connection] = {}
        for service in bt.required_connections:
            c = self.find_connection(user, service)
            if c is None or not is_connected(service, c):
                raise ConnectionRequiredError(f"Connect {service} first at /connections")
            required[service] = c
        return required

    def find_connection(self, user: User, service: str) -> Connection | None:
        """Return the operator's connection for a service, if any."""
        return (
            self.db.query(Connection)
            .filter(
                Connection.user_id == user.id,
                Connection.service == service,
            )
            .first()
        )

    def build_spawn_argv(self, deployment: Deployment, user: User) -> list[str]:
        """Build the ``kai bot start`` argv for the deployment."""
        argv = [
            *kai_argv_prefix(),
            "bot",
            "start",
            deployment.bot_type,
            "--user",
            user.email,
            "--goal",
            deployment.goal,
            "--language",
            deployment.language,
            "--template",
            deployment.template,
        ]
        overrides = deployment.tool_overrides or {}
        for t in overrides.get("enable", []):
            argv += ["--enable-tools", t]
        for t in overrides.get("disable", []):
            argv += ["--disable-tools", t]
        return argv

    def build_spawn_env(
        self,
        deployment: Deployment,
        bt: BotType,
        user: User,
        required_conns: dict[str, Connection],
        brain_conn: Connection | None,
    ) -> dict[str, str]:
        """Build the spawn environment from connections, Brain, and tools."""
        return deployment_env.build_spawn_env(
            deployment,
            bt,
            user,
            required_conns,
            brain_conn,
            db=self.db,
            allocate_control_port=self.allocate_control_port,
            inject_connection=inject_connection_env,
            inject_brain=self.inject_brain_env,
            inject_optional=self.inject_optional_tools_env,
        )

    def inject_brain_env(
        self, env: dict[str, str], deployment: Deployment, brain_conn: Connection
    ) -> None:
        """Inject Brain workspace, instruction, and mandatory env vars."""
        deployment_env.inject_brain_env(env, deployment, brain_conn)

    def inject_optional_tools_env(
        self,
        env: dict[str, str],
        deployment: Deployment,
        bt: BotType,
        user: User,
    ) -> None:
        """Inject env vars for enabled optional tool connections."""
        deployment_env.inject_optional_tools_env(
            env, deployment, bt, user, self.find_connection, inject_connection_env
        )

    def spawn_and_await_run_id(
        self,
        deployment: Deployment,
        user: User,
        argv: list[str],
        env: dict[str, str],
    ) -> str:
        """Spawn the bot, drain its output, and wait for its run id."""
        return deployment_process.spawn_and_await_run_id(self, deployment, user, argv, env)

    def wait_for_run_id(
        self,
        proc: subprocess.Popen,
        run_id_found: threading.Event,
        run_id_box: list[str],
        deadline: float,
    ) -> str:
        """Block until the subprocess prints ``KAI_RUN_ID`` or fails."""
        return deployment_process.wait_for_run_id(proc, run_id_found, run_id_box, deadline)

    def await_registry_entry(self, deployment: Deployment, user: User, run_id: str) -> None:
        """Wait for the spawned run to appear in the run registry."""
        deployment_process.await_registry_entry(self, deployment, user, run_id)

    def clear_control_port(self, deployment: Deployment) -> None:
        """Drop the allocated control port from deployment settings."""
        deployment_process.clear_control_port(deployment)

    def stop(self, deployment: Deployment) -> None:
        """Stop the bot process and mark the deployment stopped."""
        self._run_cache.clear()
        deployment_process.stop(self, deployment, kill=os.kill)

    def delete(self, deployment: Deployment) -> None:
        """Delete a deployment: stop if running, purge per-bot state, remove row."""
        self._run_cache.clear()
        deployment_process.delete(self, deployment)

    def purge_bot_state(self, deployment: Deployment) -> None:
        """Remove every per-bot state file for this deployment instance."""
        deployment_process.purge_bot_state(self, deployment)

    def run_started_at(self, deployment: Deployment) -> str | None:
        """Return the live run's start timestamp, if any."""
        return deployment_process.run_started_at(self, deployment)

    def fetch_status(self, deployment: Deployment) -> dict | None:
        """Fetch live status from the running bot, or None if stopped."""
        return deployment_process.fetch_status(self, deployment)

    def send_message(self, deployment: Deployment, message: str, persist: bool = False) -> dict:
        """Forward an operator message to the running bot's /tell route."""
        return deployment_chat.send_message(self, deployment, message, persist)

    def clear_history(self, deployment: Deployment) -> dict:
        """Clear the bot's conversation history."""
        return deployment_chat.clear_history(self, deployment)

    def history(self, deployment: Deployment) -> dict[str, list[dict]]:
        """Load the deployment's merged history (see deployment_chat.history)."""
        return deployment_chat.history(self, deployment)

    def load_history_raw(self, deployment: Deployment, instance_id: str | None = None) -> object:
        """Read the merged per-conversation and legacy history sources."""
        return deployment_chat.load_history_raw(self, deployment, instance_id)

    def normalize_history(self, history_data: dict, instance_id: str) -> dict[str, list[dict]]:
        """Strip the instance prefix and normalize stored messages."""
        return deployment_chat.normalize_history(history_data, instance_id)

    def interaction_summary(self, deployment: Deployment) -> tuple[int, int]:
        """Return ``(conversation count, message count)`` from on-disk history."""
        return deployment_chat.interaction_summary(self, deployment)


# Dispatch table for DeploymentsService.edit — maps a field name to the
# method that applies it. Methods return True when the edit changes
# ``deployment.settings`` (so the language resync below runs).
FIELD_APPLIERS: dict[str, Callable[[DeploymentsService, Deployment, BotType, object], bool]] = {
    "goal": DeploymentsService.apply_goal,
    "language": DeploymentsService.apply_language,
    "feature_flags": DeploymentsService.apply_feature_flags,
    "settings": DeploymentsService.apply_settings,
    "brain_mandatory": DeploymentsService.apply_brain_mandatory,
    "brain_instruction": DeploymentsService.apply_brain_instruction,
    "template": DeploymentsService.apply_template,
    "tool_overrides": DeploymentsService.apply_tool_overrides,
}


def reconcile_deployments(db: Session) -> None:
    """Restart any deployment whose ``desired_state`` is ``"running"`` but
    whose bot process isn't actually alive.

    ``status`` reflects *live* process state; ``desired_state`` persists the
    user's *intent*. A container restart kills every spawned bot subprocess,
    so without this reconciliation every previously running deployment would
    stay stopped until a human re-clicked Start.

    Call this once at cockpit startup (see ``app.py``'s startup hook), passing
    an open session. Each deployment is independent: a failure to restart one
    is logged and skipped rather than aborting the rest.
    """
    svc = DeploymentsService(db)

    # Reclaim stale ports for crashed bots before the restart loop.
    stale = db.query(Deployment).filter(Deployment.status == "running").all()
    for dep in stale:
        if svc.fetch_status(dep) is None:
            dep.status = "stopped"
            dep.needs_restart = False
            svc.clear_control_port(dep)
            dep.updated_at = now_iso()
            logger.info("reconcile: cleared stale state for crashed deployment %s", dep.id)
    db.commit()

    deployments = db.query(Deployment).filter(Deployment.desired_state == "running").all()
    for dep in deployments:
        try:
            if svc.fetch_status(dep) is not None:
                continue  # already alive — nothing to do
        except Exception:
            # Don't fall through to start(): if the bot is actually running
            # but the status check failed (network error, registry
            # corruption), spawning a second subprocess would leak a
            # duplicate bot. Treat the probe failure as "leave it alone".
            logger.exception("reconcile: fetch_status failed for deployment %s", dep.id)
            continue

        try:
            svc.start(dep)
            logger.info("reconcile: restarted deployment %s", dep.id)
        except ConnectionRequiredError:
            logger.warning(
                "reconcile: skipping deployment %s — required connection missing", dep.id
            )
        except DeploymentStartupError as exc:
            logger.warning("reconcile: failed to restart deployment %s: %s", dep.id, exc)
        except Exception:
            logger.exception("reconcile: unexpected error restarting deployment %s", dep.id)
