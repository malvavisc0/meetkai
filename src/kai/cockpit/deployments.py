"""Deployments service: shared code path for CLI and web.

Authorization scope differs (CLI: admin/any user; web: self only), but both
call the same methods.
"""

import importlib
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from kai.cockpit import config_writer
from kai.cockpit.bots import (
    ALL_LANGUAGES,
    BOT_TYPES,
    WEBHOOK_CONNECTION_TYPES,
    BotType,
)
from kai.cockpit.models import Connection, Deployment, User
from kai.runs import RunRegistry, pid_alive, runs_path
from kai.utils.common import compute_hmac, now_iso

logger = logging.getLogger(__name__)


def _require_supported_language(language: str) -> None:
    if language not in ALL_LANGUAGES:
        raise ValueError(f"unsupported language: {language!r}. Supported: {ALL_LANGUAGES}")


def _require_non_empty(value: object, label: str) -> None:
    if not value or not str(value).strip():
        raise ValueError(f"{label} is required")


def _validate_create_inputs(user: User, bot_type: str, goal: str, language: str) -> None:
    if user.is_disabled:
        raise ValueError(f"Operator '{user.email}' is disabled")
    if bot_type not in BOT_TYPES:
        raise ValueError(f"unknown bot type: {bot_type}")
    _require_non_empty(goal, "goal")
    _require_non_empty(language, "language")
    _require_supported_language(language.strip())


def _kai_argv_prefix() -> list[str]:
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


def _instance_id(bot_type: str, email: str) -> str:
    return f"{bot_type}-{email}"


def _tool_enabled(value: dict) -> bool:
    """Read the ``enabled`` flag from a stored tool toggle."""
    return bool(value.get("enabled", False))


def _tool_instruction(value: dict) -> str:
    return str(value.get("instruction", ""))


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


def _inject_connection_env(env: dict, service: str, conn: Connection) -> bool:
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
        return _apply_service_env_vars(env, cfg, svc_vars)
    except DeploymentStartupError:
        raise
    except NotImplementedError:
        raise
    except Exception as exc:
        raise DeploymentStartupError(
            f"could not decrypt {service} connection — reconfigure at /connections/{service}"
        ) from exc


def _apply_service_env_vars(env: dict, cfg: dict, svc_vars: dict) -> bool:
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


class DeploymentsService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _user_for(self, deployment: Deployment) -> User:
        user = self.db.query(User).filter(User.id == deployment.user_id).first()
        if user is None:
            raise ValueError(f"deployment {deployment.id} has no Operator")
        return user

    def _instance_id(self, deployment: Deployment, *, user: User | None = None) -> str:
        if user is None:
            user = self._user_for(deployment)
        return _instance_id(deployment.bot_type, user.email)

    def _allocate_control_port(self, db: Session, user: User) -> int:
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

    def _registry(self, deployment: Deployment, *, user: User | None = None) -> RunRegistry:
        from kai.config.settings import get_settings

        settings = get_settings()
        instance_id = self._instance_id(deployment, user=user)
        return RunRegistry(runs_path(settings.agent_history_folder, instance_id))

    def _resolve_run(self, deployment: Deployment):
        if not deployment.run_id:
            return None
        registry = self._registry(deployment)
        return registry.get(deployment.run_id)

    def _compute_hmac(self, record, body: bytes) -> str:
        return compute_hmac(record.hmac_key, body, record.hmac_algorithm)

    def _call_bot(
        self,
        record,
        method: str,
        path: str,
        body: bytes = b"",
        *,
        timeout: float = 30.0,
    ) -> dict:
        """Make an HMAC-signed HTTP call to the running bot.

        Returns the JSON response dict on success, or an error dict on failure.
        """
        signature = self._compute_hmac(record, body)
        headers: dict[str, str] = {"X-Webhook-Hmac": signature}
        if body:
            headers["Content-Type"] = "application/json"
        try:
            resp = httpx.request(
                method,
                f"{record.endpoint}{path}",
                content=body if body else None,
                headers=headers,
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": str(exc)}

    def forward_event(self, deployment: Deployment, path: str, body: bytes) -> bool:
        """Forward a normalized inbound event to a running deployment's bot.

        Returns True if the bot accepted the event, False if the bot isn't
        reachable or rejected it. ``_call_bot`` returns ``{"ok": False, ...}``
        on every failure shape, so ``False`` is the one failure signal.
        """
        record = self._resolve_run(deployment)
        if record is None:
            return False
        result = self._call_bot(record, "POST", path, body)
        return result.get("ok", True) is not False

    def get(self, deployment_id: int) -> Deployment | None:
        return self.db.query(Deployment).filter(Deployment.id == deployment_id).first()

    def list_for_user(self, user_id: int) -> list[Deployment]:
        return self.db.query(Deployment).filter(Deployment.user_id == user_id).all()

    def get_for_user_and_type(self, user_id: int, bot_type: str) -> Deployment | None:
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
        _validate_create_inputs(user, bot_type, goal, language)
        bt = BOT_TYPES[bot_type]
        self._require_connections(bt, user)
        self._reject_duplicate(bot_type, user)

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

    def _require_connections(self, bt: BotType, user: User) -> None:
        for service in bt.required_connections:
            c = self._find_connection(user, service)
            if not is_connected(service, c):
                raise ConnectionRequiredError(f"Connect {service} first at /connections")

    def _reject_duplicate(self, bot_type: str, user: User) -> None:
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
        bt = BOT_TYPES.get(deployment.bot_type)
        if bt is None:
            raise ValueError(f"unknown bot type: {deployment.bot_type}")

        settings_changed = False
        apply = _FIELD_APPLIERS
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
            config_writer.write_config(deployment, self._instance_id(deployment))
        except OSError:
            logger.warning("Failed to write config for deployment %s", deployment.id, exc_info=True)

        self.db.commit()
        return deployment

    def _apply_goal(self, deployment: Deployment, bt: BotType, value: object) -> bool:
        _require_non_empty(value, "goal")
        deployment.goal = str(value).strip()
        return False

    def _apply_language(self, deployment: Deployment, bt: BotType, value: object) -> bool:
        _require_non_empty(value, "language")
        language = str(value).strip()
        _require_supported_language(language)
        deployment.language = language
        deployment.settings["language"] = deployment.language
        return True

    def _apply_feature_flags(self, deployment: Deployment, bt: BotType, value: object) -> bool:
        if not isinstance(value, dict):
            raise ValueError("feature_flags must be a dict")
        invalid = set(value.keys()) - set(bt.feature_flags)
        if invalid:
            raise ValueError(f"invalid feature flags: {invalid}")
        deployment.feature_flags = value
        return False

    def _apply_settings(self, deployment: Deployment, bt: BotType, value: object) -> bool:
        if not isinstance(value, dict):
            raise ValueError("settings must be a dict")
        for req in bt.required_settings:
            if req in value and not value[req]:
                raise ValueError(f"setting '{req}' cannot be empty")
        deployment.settings = {**deployment.settings, **value}
        return True

    def _apply_brain_mandatory(self, deployment: Deployment, bt: BotType, value: object) -> bool:
        if value is not None and not isinstance(value, bool):
            raise ValueError("brain_mandatory must be a bool or null")
        deployment.brain_mandatory = value
        return False

    def _apply_brain_instruction(self, deployment: Deployment, bt: BotType, value: object) -> bool:
        if value is not None and not isinstance(value, str):
            raise ValueError("brain_instruction must be a string or null")
        deployment.brain_instruction = str(value).strip() if value is not None else None
        return False

    def _apply_template(self, deployment: Deployment, bt: BotType, value: object) -> bool:
        deployment.template = str(value)
        return False

    def _apply_tool_overrides(self, deployment: Deployment, bt: BotType, value: object) -> bool:
        if not isinstance(value, dict):
            raise ValueError("tool_overrides must be a dict")
        if set(value.keys()) - {"enable", "disable"}:
            raise ValueError("tool_overrides must only have 'enable' and 'disable' keys")
        deployment.tool_overrides = {
            "enable": list(value.get("enable", [])),
            "disable": list(value.get("disable", [])),
        }
        return False

    def start(self, deployment: Deployment) -> None:
        """Start a deployment: check connection, write config, spawn subprocess."""
        bt = BOT_TYPES.get(deployment.bot_type)
        if bt is None:
            raise ValueError(f"unknown bot type: {deployment.bot_type}")

        user = self._user_for(deployment)
        instance_id = self._instance_id(deployment, user=user)

        required_conns = self._resolve_required_connections(bt, user)
        brain_conn = self._find_connection(user, "morphik")

        config_writer.write_config(deployment, instance_id)

        argv = self._build_spawn_argv(deployment, user)
        env = self._build_spawn_env(deployment, bt, user, required_conns, brain_conn)

        run_id = self._spawn_and_await_run_id(deployment, user, argv, env)

        deployment.run_id = run_id
        deployment.status = "running"
        deployment.desired_state = "running"
        deployment.needs_restart = False
        deployment.updated_at = now_iso()
        self.db.commit()

    def _resolve_required_connections(self, bt: BotType, user: User) -> dict[str, Connection]:
        required: dict[str, Connection] = {}
        for service in bt.required_connections:
            c = self._find_connection(user, service)
            if c is None or not is_connected(service, c):
                raise ConnectionRequiredError(f"Connect {service} first at /connections")
            required[service] = c
        return required

    def _find_connection(self, user: User, service: str) -> Connection | None:
        return (
            self.db.query(Connection)
            .filter(
                Connection.user_id == user.id,
                Connection.service == service,
            )
            .first()
        )

    def _build_spawn_argv(self, deployment: Deployment, user: User) -> list[str]:
        argv = [
            *_kai_argv_prefix(),
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

    def _build_spawn_env(
        self,
        deployment: Deployment,
        bt: BotType,
        user: User,
        required_conns: dict[str, Connection],
        brain_conn: Connection | None,
    ) -> dict[str, str]:
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
                _inject_connection_env(env, service, c)
            except DeploymentStartupError as exc:
                raise ConnectionRequiredError(f"{service} config unreadable: {exc}") from exc

        # Control-port + HMAC-key injection for bot types. The port is stored
        # in Deployment.settings["control_port"] (JSON, no migration) and
        # cleared on stop(). This is the generic path — the next webhook bot
        # needs zero cockpit changes.
        control_port = self._allocate_control_port(self.db, user)
        env["KAI_BOT_CONTROL_PORT"] = str(control_port)
        env["KAI_BOT_CONTROL_HOST"] = "0.0.0.0"
        env["KAI_BOT_HMAC_KEY"] = user.hmac_key
        env["KAI_CONFIGS_DIR"] = str(config_writer.CONFIGS_DIR)
        deployment.settings = {**deployment.settings, "control_port": control_port}

        if brain_conn is not None:
            self._inject_brain_env(env, deployment, brain_conn)

        self._inject_optional_tools_env(env, deployment, bt, user)
        return env

    def _inject_brain_env(
        self, env: dict[str, str], deployment: Deployment, brain_conn: Connection
    ) -> None:
        workspace = brain_conn.config["workspace"]
        if deployment.brain_instruction is not None and deployment.brain_instruction.strip():
            instruction = deployment.brain_instruction
        else:
            instruction = brain_conn.config.get("instruction", "")
        mandatory = deployment.brain_mandatory is True
        env["KAI_BRAIN_WORKSPACE"] = workspace
        env["KAI_BRAIN_INSTRUCTION"] = instruction
        env["KAI_BRAIN_MANDATORY"] = "true" if mandatory else "false"

    def _inject_optional_tools_env(
        self,
        env: dict[str, str],
        deployment: Deployment,
        bt: BotType,
        user: User,
    ) -> None:
        tools_cfg = deployment.settings.get("tools", {})
        for service in bt.supported_connections:
            if service in bt.required_connections:
                continue
            if not _tool_enabled(tools_cfg.get(service, {})):
                continue
            c = self._find_connection(user, service)
            if c is None:
                continue
            try:
                _inject_connection_env(env, service, c)
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
                env[instr_var] = _tool_instruction(tools_cfg.get(service, {}))

    def _spawn_and_await_run_id(
        self,
        deployment: Deployment,
        user: User,
        argv: list[str],
        env: dict[str, str],
    ) -> str:
        proc = subprocess.Popen(
            argv,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        run_id: str | None = None
        run_id_found = threading.Event()
        run_id_box: list[str] = []
        deadline = time.time() + 30
        stdout = proc.stdout
        assert stdout is not None, "subprocess.PIPE was set; stdout must be non-None"

        # Drain stdout/stderr for the entire subprocess lifetime. Bot output
        # was previously only read up to the first "KAI_RUN_ID=" line, then
        # abandoned — once the bot's own logging filled the OS pipe buffer
        # (typically 64KB), the next write() would block forever, hanging the bot.
        instance_id_for_logs = self._instance_id(deployment, user=user)

        def _pump_output() -> None:
            try:
                for line in iter(stdout.readline, ""):
                    line = line.rstrip("\n")
                    if not run_id_found.is_set() and "KAI_RUN_ID=" in line:
                        run_id_box.append(line.strip().split("KAI_RUN_ID=")[1].split()[0])
                        run_id_found.set()
                    if line:
                        print(f"[{instance_id_for_logs}] {line}", flush=True)
            except (ValueError, OSError):
                pass

        pump_thread = threading.Thread(
            target=_pump_output,
            name=f"bot-output-{instance_id_for_logs}",
            daemon=True,
        )
        pump_thread.start()

        try:
            run_id = self._wait_for_run_id(proc, run_id_found, run_id_box, deadline)
            self._await_registry_entry(deployment, user, run_id)
        except DeploymentStartupError:
            if hasattr(proc, "kill"):
                proc.kill()
            if hasattr(proc, "wait"):
                proc.wait(timeout=5)
            raise
        return run_id

    def _wait_for_run_id(
        self,
        proc: subprocess.Popen,
        run_id_found: threading.Event,
        run_id_box: list[str],
        deadline: float,
    ) -> str:
        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            if run_id_found.wait(timeout=min(remaining, 0.5)):
                return run_id_box[0]
            if proc.poll() is not None:
                raise DeploymentStartupError(f"Process exited with code {proc.returncode}")

        raise DeploymentStartupError("Timed out waiting for KAI_RUN_ID")

    def _await_registry_entry(self, deployment: Deployment, user: User, run_id: str) -> None:
        registry = self._registry(deployment, user=user)
        deadline = time.time() + 10
        while time.time() < deadline:
            if registry.get(run_id) is not None:
                return
            time.sleep(0.5)
        raise DeploymentStartupError(f"Run {run_id} not found in registry")

    def _clear_control_port(self, deployment: Deployment) -> None:
        if "control_port" in deployment.settings:
            deployment.settings = {
                k: v for k, v in deployment.settings.items() if k != "control_port"
            }

    def stop(self, deployment: Deployment) -> None:
        if not deployment.run_id:
            deployment.status = "stopped"
            deployment.desired_state = "stopped"
            deployment.needs_restart = False
            self._clear_control_port(deployment)
            deployment.updated_at = now_iso()
            self.db.commit()
            return

        registry = self._registry(deployment)
        record = registry.get(deployment.run_id)

        if record and pid_alive(record.pid):
            try:
                os.kill(record.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            for _ in range(20):
                if not pid_alive(record.pid):
                    break
                time.sleep(0.5)
            else:
                if pid_alive(record.pid):
                    try:
                        os.kill(record.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    time.sleep(0.5)

        registry.remove(deployment.run_id)
        deployment.run_id = None
        deployment.status = "stopped"
        deployment.desired_state = "stopped"
        deployment.needs_restart = False
        self._clear_control_port(deployment)
        deployment.updated_at = now_iso()
        self.db.commit()

    def delete(self, deployment: Deployment) -> None:
        """Delete a deployment: stop if running, purge per-bot state, remove row."""
        if deployment.status == "running" or deployment.run_id:
            self.stop(deployment)

        self._purge_bot_state(deployment)

        self.db.delete(deployment)
        self.db.commit()

    def _purge_bot_state(self, deployment: Deployment) -> None:
        """Remove every per-bot state file for this deployment instance."""
        instance_id = self._instance_id(deployment)

        from kai.config.settings import get_settings

        settings = get_settings()

        # The suffixes below are hardcoded to match independent producers with no
        # shared naming helper: KaiAgent._resolve_history_file (history/goal),
        # runs.runs_path (runs), kai.bots.base.Bot (tasks). The seen/sleep
        # suffixes purge leftover state files from removed bot features so they
        # don't linger on disk. If any naming rule changes, update the suffixes
        # here too or purge silently leaves orphaned files behind.
        history_suffixes = [
            f"{instance_id}.json",
            f"{instance_id}.json.goal",
        ]
        task_suffixes = [
            f"{instance_id}.seen.json",
            f"{instance_id}.sleep.json",
            f"{instance_id}.tasks.json",
        ]

        if settings.agent_history_folder is not None:
            history_folder = Path(settings.agent_history_folder)
            for suffix in history_suffixes:
                try:
                    (history_folder / suffix).unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                runs_path(settings.agent_history_folder, instance_id).unlink(missing_ok=True)
            except OSError:
                pass

        tasks_folder = Path(settings.tasks_folder)
        for suffix in task_suffixes:
            try:
                (tasks_folder / suffix).unlink(missing_ok=True)
            except OSError:
                pass

        try:
            (config_writer.CONFIGS_DIR / f"{instance_id}.json").unlink(missing_ok=True)
        except OSError:
            pass

    def run_started_at(self, deployment: Deployment) -> str | None:
        record = self._resolve_run(deployment)
        return record.started_at if record else None

    def fetch_status(self, deployment: Deployment) -> dict | None:
        """Fetch live status from the running bot, or None if stopped."""
        record = self._resolve_run(deployment)
        if record is None:
            return None

        result = self._call_bot(record, "GET", "/status", timeout=10.0)
        if "error" in result and not result.get("ok", True):
            return None
        return result

    def send_message(self, deployment: Deployment, message: str, persist: bool = False) -> dict:
        """Forward an operator message to the running bot's /tell route."""
        record = self._resolve_run(deployment)
        if record is None:
            return {"ok": False, "reply": "bot is not running"}

        body = json.dumps({"message": message, "persist": persist}).encode()
        return self._call_bot(record, "POST", "/tell", body, timeout=120.0)

    def clear_history(self, deployment: Deployment) -> dict:
        record = self._resolve_run(deployment)
        if record is None:
            return {"ok": False, "error": "bot is not running"}

        return self._call_bot(record, "POST", "/clear")

    def history(self, deployment: Deployment) -> dict[str, list[dict]]:
        """Load the per-bot history file and return ``{chat_id: [messages]}``.

        Each message is ``{"role": str, "content": str, "ts": str | None}``.
        ``ts`` is the ISO-8601 UTC timestamp recorded when the message was
        stored; older history files (pre-timestamp) have ``None``. Returns
        ``{}`` when the file is missing or unreadable. The history file is
        written atomically by the bot process (a ``.tmp`` replace), so
        reading it from the cockpit is safe.

        On-disk keys are namespaced as ``{instance_id}:{conversation_id}``
        (see :meth:`KaiAgent._history_key``); the returned dict strips the
        instance prefix so the cockpit surfaces the raw conversation id
        (e.g. ``operator``, ``1809...@g.us``).
        """
        instance_id = self._instance_id(deployment)
        raw = self._load_history_raw(deployment, instance_id)
        if not isinstance(raw, dict):
            return {}
        history_data = raw.get("history", raw)
        if not isinstance(history_data, dict):
            return {}
        return self._normalize_history(history_data, instance_id)

    def _load_history_raw(self, deployment: Deployment, instance_id: str | None = None) -> object:
        from kai.config.settings import get_settings

        folder = get_settings().agent_history_folder
        if folder is None:
            return {}
        iid = instance_id or self._instance_id(deployment)
        path = Path(folder) / f"{iid}.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("failed to read history file %s", path)
            return {}

    @staticmethod
    def _normalize_history(history_data: dict, instance_id: str) -> dict[str, list[dict]]:
        prefix = f"{instance_id}:"
        result: dict[str, list[dict]] = {}
        for key, messages in history_data.items():
            if not isinstance(key, str) or not isinstance(messages, list):
                continue
            chat_id = key[len(prefix) :] if key.startswith(prefix) else key
            result[chat_id] = [_normalize_msg(m) for m in messages if _valid_msg(m)]
        return result

    def interaction_summary(self, deployment: Deployment) -> tuple[int, int]:
        history = self.history(deployment)
        conversation_count = len(history)
        message_count = sum(len(msgs) for msgs in history.values())
        return conversation_count, message_count


# Dispatch table for DeploymentsService.edit — maps a field name to the
# method that applies it. Methods return True when the edit changes
# ``deployment.settings`` (so the language resync below runs).
_FIELD_APPLIERS: dict[str, Callable[[DeploymentsService, Deployment, BotType, object], bool]] = {
    "goal": DeploymentsService._apply_goal,
    "language": DeploymentsService._apply_language,
    "feature_flags": DeploymentsService._apply_feature_flags,
    "settings": DeploymentsService._apply_settings,
    "brain_mandatory": DeploymentsService._apply_brain_mandatory,
    "brain_instruction": DeploymentsService._apply_brain_instruction,
    "template": DeploymentsService._apply_template,
    "tool_overrides": DeploymentsService._apply_tool_overrides,
}


def reconcile_deployments() -> None:
    """Restart any deployment whose ``desired_state`` is ``"running"`` but
    whose bot process isn't actually alive.

    ``status`` reflects *live* process state; ``desired_state`` persists the
    user's *intent*. A container restart kills every spawned bot subprocess,
    so without this reconciliation every previously running deployment would
    stay stopped until a human re-clicked Start.

    Call this once at cockpit startup (see ``app.py``'s startup hook). Each
    deployment is independent: a failure to restart one is logged and skipped
    rather than aborting the rest.
    """
    from kai.cockpit.db import SessionLocal

    db = SessionLocal()
    try:
        svc = DeploymentsService(db)

        # Reclaim stale ports for crashed bots before the restart loop.
        stale = db.query(Deployment).filter(Deployment.status == "running").all()
        for dep in stale:
            if svc.fetch_status(dep) is None:
                dep.status = "stopped"
                dep.needs_restart = False
                svc._clear_control_port(dep)
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
    finally:
        db.close()


def topbar_status(request, user) -> str:
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
    return _topbar_level(running, unexpected_down)


def _topbar_level(running: int, unexpected_down: int) -> str:
    if unexpected_down:
        return "down" if running == 0 else "warn"
    return "ok" if running > 0 else "warn"


def _valid_msg(m: object) -> bool:
    return isinstance(m, dict) and "role" in m and "content" in m


def _normalize_msg(m: dict) -> dict:
    ts = m.get("ts")
    return {
        "role": str(m["role"]),
        "content": str(m["content"]),
        "ts": ts if isinstance(ts, str) else None,
    }
