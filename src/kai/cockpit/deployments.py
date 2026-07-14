"""Shared deployments service — single code path for CLI and web.

Only authorization scope differs between CLI (admin, any user) and web
(self only). Both call the same methods.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from kai.cockpit import config_writer
from kai.cockpit.bots import BOT_TYPES, auto_pick_voice
from kai.cockpit.models import Connection, Deployment, User
from kai.runs import RunRegistry, pid_alive, runs_path
from kai.utils.common import compute_hmac, now_iso

logger = logging.getLogger(__name__)


def _kai_argv_prefix() -> list[str]:
    """Resolve the `kai` executable for spawning bot subprocesses.

    Inside the Docker image (and any install where the package is
    installed into the venv) the `kai` console script is on PATH — use it
    directly, with no `uv run` wrapper, so each spawn is fast and doesn't
    re-resolve dependencies. In a bare-metal dev checkout run via
    ``uv run kai cockpit serve`` the console script may be absent, so fall
    back to ``["uv", "run", "kai"]``.
    """
    if shutil.which("kai"):
        return ["kai"]
    return ["uv", "run", "kai"]


class ConnectionRequiredError(Exception):
    """Raised when a deployment is created or started without a required
    connection connected (see ``BotType.required_connections``)."""


class DeploymentStartupError(Exception):
    """Raised when the bot subprocess fails to start or register."""


def attention_reason(
    dep: Deployment, status_data: dict | None, whatsapp_connected: bool
) -> str | None:
    """Why a deployment needs the operator's action *now*, or None if it doesn't.

    - WhatsApp disconnected while the bot is meant to be running (intent unmet).
    - A ``running`` row whose live status probe comes back empty (process died
      but the row wasn't reconciled — reconciliation only runs at startup).
    - A running deployment with unapplied settings changes (needs_restart).

    A failed start decays to the neutral ``stopped`` state on the next load
    rather than staying red — red is reserved for states needing action now.

    ``status_data`` is the live ``/status`` probe result for ``dep`` (``None``
    if it isn't running or the probe failed) — the caller fetches it once per
    running deployment and reuses it here and for the card's task count, so
    the route never doubles the number of live status calls.

    ``whatsapp_connected`` is the DB's ``Connection.status`` flag, which is
    only written once during the pairing/QR-scan flow and never re-verified
    afterward — it can still read "connected" long after the session (or the
    host's internet) has actually died. When ``status_data`` is available for
    a running deployment, its ``connected`` field takes precedence: it comes
    from ``status_snapshot()`` fetching the bot's own WhatsApp profile, which
    forces a real round-trip through WAHA to WhatsApp, so it reflects whether
    the session is reachable *right now* rather than a stale snapshot.

    Shared by the console list (per-card badge) and the deployment detail
    page (a banner when a running bot's WhatsApp is disconnected) — both
    routes compute this the same way, from the same inputs, so they can
    never disagree about whether a deployment needs attention.
    """
    live_connected = whatsapp_connected
    if dep.status == "running" and status_data is not None and "connected" in status_data:
        live_connected = status_data["connected"]

    if dep.desired_state == "running" and not live_connected:
        return "WhatsApp down, wants running"
    if dep.status == "running":
        if status_data is None:
            return "Bot process isn't responding"
        if dep.needs_restart:
            return "Restart needed to apply settings"
    return None


def _instance_id(bot_type: str, email: str) -> str:
    """Compute the per-bot instance namespace the spawned process uses."""
    return f"{bot_type}-{email}"


def _tool_enabled(value: bool | dict) -> bool:
    """Normalize a stored tool toggle to a boolean ``enabled`` state.

    Handles both the legacy flat bool form (``True`` / ``False``) and the
    newer nested dict form (``{"enabled": True, "instruction": "..."}``).
    """
    if isinstance(value, dict):
        return bool(value.get("enabled", False))
    return bool(value)


def _tool_instruction(value: bool | dict) -> str:
    """Extract the instruction text from a stored tool toggle (dict form)."""
    if isinstance(value, dict):
        return str(value.get("instruction", ""))
    return ""


# Maps each supported-connection service to its env-var names. ``fields``
# maps config_key → env_var_name for credential fields. ``instruction``
# is the env var that carries the operator's per-deployment usage rules.
# ``bool_fields`` lists config keys whose values should be stringified as
# "true"/"false". Adding a service here wires injection (start() loop),
# the instruction guard, and storage (via _TOOLS_WITH_INSTRUCTION in the
# routes) — a single source of truth.
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
            "username": "KAI_SMTP_TOOL_USER",
            "password": "KAI_SMTP_TOOL_PASSWORD",
            "from_address": "KAI_SMTP_TOOL_FROM",
            "use_tls": "KAI_SMTP_TOOL_USE_TLS",
        },
        "instruction": "KAI_SMTP_TOOL_INSTRUCTION",
        "bool_fields": {"use_tls"},
    },
}


def _inject_connection_env(env: dict, service: str, conn: Connection) -> bool:
    """Inject env vars for a supported credential connection into ``env``.

    Each service has a bespoke env shape, driven by ``SERVICE_ENV_VARS``.
    A service not listed there raises ``NotImplementedError`` so a
    misconfiguration surfaces immediately at start. A decryption failure
    (wrong key, tampered ciphertext) is converted to
    ``DeploymentStartupError`` so the route handler surfaces a flash
    message instead of a bare 500.

    Returns True if at least one field was injected (i.e. the credential
    had data), False if nothing was set (e.g. an empty connection row).
    """
    try:
        svc_vars = SERVICE_ENV_VARS.get(service)
        if svc_vars is None:
            raise NotImplementedError(f"env injection for {service!r} not implemented")
        from kai.cockpit.secrets import decrypt_config

        cfg = decrypt_config(service, conn.config)
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
    except DeploymentStartupError:
        raise
    except NotImplementedError:
        raise
    except Exception as exc:
        raise DeploymentStartupError(
            f"could not decrypt {service} connection — reconfigure at /connections/{service}"
        ) from exc


class DeploymentsService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _user_for(self, deployment: Deployment) -> User:
        """Resolve the deployment's Operator, raising if missing."""
        user = self.db.query(User).filter(User.id == deployment.user_id).first()
        if user is None:
            raise ValueError(f"deployment {deployment.id} has no Operator")
        return user

    def _instance_id(self, deployment: Deployment, *, user: User | None = None) -> str:
        if user is None:
            user = self._user_for(deployment)
        return _instance_id(deployment.bot_type, user.email)

    def _registry(self, deployment: Deployment, *, user: User | None = None) -> RunRegistry:
        from kai.config.settings import get_settings

        settings = get_settings()
        instance_id = self._instance_id(deployment, user=user)
        return RunRegistry(runs_path(settings.agent_history_folder, instance_id))

    def _resolve_run(self, deployment: Deployment):
        """Resolve a deployment's run_id to a RunRecord, or None."""
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
        on every failure shape (HTTP error, JSON decode), so ``False`` is the
        one failure signal; a response with no ``ok`` key at all is success.
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
        """A user's deployment for a specific bot type, or None."""
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
        voice: str | None = None,
    ) -> Deployment:
        """Create a deployment. Validates required fields. Auto-picks voice if None."""
        if user.is_disabled:
            raise ValueError(f"Operator '{user.email}' is disabled")
        if bot_type not in BOT_TYPES:
            raise ValueError(f"unknown bot type: {bot_type}")
        if not goal or not goal.strip():
            raise ValueError("goal is required")
        if not language or not language.strip():
            raise ValueError("language is required")

        bt = BOT_TYPES[bot_type]

        # Catalog-driven creation gate: every connection this bot type
        # declares as required must be present and connected *before* the
        # deployment can even be created — not just before it starts.
        # Letting an operator configure a bot it can never run is a source
        # of confusion (a "ready" looking deployment that silently can't
        # start), so the requirement is enforced at the earliest point
        # instead of deferred to start(). Mirrors the same catalog read
        # start() uses below, so the two can never disagree about what a
        # bot type requires.
        for service in bt.required_connections:
            c = (
                self.db.query(Connection)
                .filter(
                    Connection.user_id == user.id,
                    Connection.service == service,
                )
                .first()
            )
            if c is None or c.status != "connected":
                raise ConnectionRequiredError(f"Connect {service} first at /connections")

        existing = (
            self.db.query(Deployment)
            .filter(Deployment.user_id == user.id, Deployment.bot_type == bot_type)
            .first()
        )
        if existing:
            raise ValueError(
                f"Operator '{user.email}' already has a {bot_type} deployment (id={existing.id})"
            )

        if not voice or not voice.strip():
            voice = auto_pick_voice(language)

        from kai.bots.waha.setup import BotConfig

        default_config = BotConfig(language=language, timezone=user.timezone)
        settings = default_config.model_dump()

        feature_flags = {f: False for f in bt.feature_flags}

        dep = Deployment(
            user_id=user.id,
            bot_type=bot_type,
            goal=goal.strip(),
            language=language.strip(),
            voice=voice.strip(),
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

    def edit(self, deployment: Deployment, **fields: object) -> Deployment:
        """Partial update of deployment fields."""
        bt = BOT_TYPES.get(deployment.bot_type)
        if bt is None:
            raise ValueError(f"unknown bot type: {deployment.bot_type}")

        settings_changed = False

        for key, value in fields.items():
            if key == "goal":
                if not value or not str(value).strip():
                    raise ValueError("goal cannot be empty")
                deployment.goal = str(value).strip()
            elif key == "language":
                if not value or not str(value).strip():
                    raise ValueError("language cannot be empty")
                deployment.language = str(value).strip()
                deployment.settings["language"] = deployment.language
                settings_changed = True
            elif key == "voice":
                if not value or not str(value).strip():
                    raise ValueError("voice cannot be empty")
                deployment.voice = str(value).strip()
            elif key == "feature_flags":
                if not isinstance(value, dict):
                    raise ValueError("feature_flags must be a dict")
                invalid = set(value.keys()) - set(bt.feature_flags)
                if invalid:
                    raise ValueError(f"invalid feature flags: {invalid}")
                deployment.feature_flags = value
            elif key == "settings":
                if not isinstance(value, dict):
                    raise ValueError("settings must be a dict")
                for req in bt.required_settings:
                    if req in value and not value[req]:
                        raise ValueError(f"setting '{req}' cannot be empty")
                deployment.settings = {**deployment.settings, **value}
                settings_changed = True
            elif key == "brain_mandatory":
                if value is not None and not isinstance(value, bool):
                    raise ValueError("brain_mandatory must be a bool or null")
                deployment.brain_mandatory = value
            elif key == "brain_instruction":
                if value is not None and not isinstance(value, str):
                    raise ValueError("brain_instruction must be a string or null")
                deployment.brain_instruction = str(value).strip() if value is not None else None

        # Resolve any language conflict: an explicit ``language`` argument
        # always wins over a value inside ``settings["language"]``. Without
        # this, ``edit(language="English", settings={"language": "Spanish"})``
        # would leave the column ("English") and the config file ("Spanish")
        # disagreeing — and the bot reads language from the CLI flag (column)
        # while the config file is what gets written to disk.
        if "language" in fields:
            deployment.settings["language"] = deployment.language
        elif settings_changed:
            # Defensive: only reachable if a future caller passes ``settings``
            # without ``language``. Current callers (web routes, CLI) always
            # pass ``language`` explicitly.
            deployment.language = deployment.settings.get("language", deployment.language)

        # A settings/goal/language/voice/feature-flags edit while the bot is
        # running leaves the live process with stale in-memory config (the
        # config file is written to disk immediately below). Flag it so the
        # detail page and console can show a durable "restart to apply"
        # badge that survives reloads — the prior session-flash signal was
        # lost on reload/new tab. Cleared on the next start()/stop().
        if deployment.status == "running":
            deployment.needs_restart = True

        deployment.updated_at = now_iso()
        self.db.commit()

        try:
            config_writer.write_config(deployment, self._instance_id(deployment))
        except OSError:
            logger.warning("Failed to write config for deployment %s", deployment.id, exc_info=True)

        return deployment

    def start(self, deployment: Deployment) -> None:
        """Start a deployment: check connection, write config, spawn subprocess."""
        from kai.bots.waha.config import get_waha_settings
        from kai.cockpit.media_services import MEDIA_READY

        # Bounded gate: block briefly for a still-loading media service
        # rather than failing instantly, but never wedge the request forever.
        timeout = get_waha_settings().media_ready_timeout
        if not MEDIA_READY.wait(timeout=timeout):
            raise DeploymentStartupError(
                f"media services not ready after waiting {timeout}s — "
                "STT/TTS servers have not started yet"
            )

        user = self._user_for(deployment)
        instance_id = self._instance_id(deployment, user=user)

        bt = BOT_TYPES.get(deployment.bot_type)
        if bt is None:
            raise ValueError(f"unknown bot type: {deployment.bot_type}")

        # Catalog-driven start gate: every connection this bot type
        # declares as required must be present and connected. Replaces the
        # prior literal ``Connection.service == "whatsapp"`` filter — a
        # second bot type needing a different connection can now declare it.
        required_conns: dict[str, Connection] = {}
        for service in bt.required_connections:
            c = (
                self.db.query(Connection)
                .filter(
                    Connection.user_id == user.id,
                    Connection.service == service,
                )
                .first()
            )
            if c is None or c.status != "connected":
                raise ConnectionRequiredError(f"Connect {service} first at /connections")
            required_conns[service] = c

        conn = required_conns.get("whatsapp")

        # Brain (lightrag) — a second, NON-fatal connection lookup (unlike
        # WhatsApp above). A Brain is never a hard prerequisite for a bot to
        # run: if the Operator has not created the Brain yet, the bot simply
        # starts without Brain memory enabled.
        brain_conn = (
            self.db.query(Connection)
            .filter(
                Connection.user_id == user.id,
                Connection.service == "lightrag",
            )
            .first()
        )

        config_writer.write_config(deployment, instance_id)

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
            "--voice",
            deployment.voice,
        ]

        env: dict[str, str] = {**os.environ}
        # WAHA-specific env shape — bespoke to the waha bot type.
        # ``conn`` is the whatsapp Connection from the required-connections
        # gate; it's None for a future non-whatsapp bot type, which would
        # have its own bespoke env block here instead.
        if conn is not None:
            env["KAI_WAHA_SESSION"] = conn.config["waha_session"]
            env["KAI_WAHA_HMAC_KEY"] = user.hmac_key
            env["KAI_WAHA_WEBHOOK_PORT"] = str(conn.config["waha_webhook_port"])
            env["KAI_WAHA_WEBHOOK_HOST"] = "0.0.0.0"
            env["KAI_WAHA_WEBHOOK_PUBLIC_HOST"] = os.environ.get("KAI_WAHA_WEBHOOK_PUBLIC_HOST", "")
            env["KAI_WAHA_WEBHOOK_PATH"] = conn.config["waha_webhook_path"]
            env["KAI_WAHA_WHISPER_SERVER_HOST"] = os.environ.get(
                "KAI_WAHA_WHISPER_SERVER_HOST", "127.0.0.1"
            )
            env["KAI_WAHA_WHISPER_SERVER_PORT"] = os.environ.get(
                "KAI_WAHA_WHISPER_SERVER_PORT", "8787"
            )
            env["KAI_WAHA_KOKORO_SERVER_HOST"] = os.environ.get(
                "KAI_WAHA_KOKORO_SERVER_HOST", "127.0.0.1"
            )
            env["KAI_WAHA_KOKORO_SERVER_PORT"] = os.environ.get(
                "KAI_WAHA_KOKORO_SERVER_PORT", "8788"
            )
            voice_map = deployment.settings.get("kokoro_voice_map", "")
            if voice_map:
                env["KAI_WAHA_KOKORO_VOICE_MAP"] = voice_map
            env["KAI_CONFIGS_DIR"] = "data/configs/cockpit"
        if brain_conn is not None:
            workspace = brain_conn.config["workspace"]
            if deployment.brain_instruction is not None and deployment.brain_instruction.strip():
                instruction = deployment.brain_instruction
            else:
                instruction = brain_conn.config.get("instruction", "")
            mandatory = deployment.brain_mandatory is True
            env["KAI_BRAIN_WORKSPACE"] = workspace
            env["KAI_BRAIN_INSTRUCTION"] = instruction
            env["KAI_BRAIN_MANDATORY"] = "true" if mandatory else "false"

        # Supported-connection injection: for each optional connection this
        # bot type declares, inject its env vars only when both the operator
        # has toggled it on for this deployment (settings["tools"]) AND the
        # Connection row exists. Never "on for every bot just because the
        # operator connected it once." The stored value may be a plain bool
        # (simple toggle) or a nested dict (toggle + instruction).
        tools_cfg = deployment.settings.get("tools", {})
        for service in bt.supported_connections:
            if service in bt.required_connections:
                continue
            if not _tool_enabled(tools_cfg.get(service, False)):
                continue
            c = (
                self.db.query(Connection)
                .filter(
                    Connection.user_id == user.id,
                    Connection.service == service,
                )
                .first()
            )
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
            # Per-tool instruction. Only inject if at least one credential
            # field was actually set — a connection with no data shouldn't
            # produce a ghost instruction the bot will silently ignore.
            svc_vars = SERVICE_ENV_VARS.get(service, {})
            instr_var = svc_vars.get("instruction")
            if instr_var and any(ev in env for ev in svc_vars.get("fields", {}).values()):
                env[instr_var] = _tool_instruction(tools_cfg.get(service))

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
        # ``stdout`` is piped above (subprocess.PIPE), so it is non-None at
        # runtime; the type stub still allows None, so narrow once here.
        stdout = proc.stdout
        assert stdout is not None, "subprocess.PIPE was set; stdout must be non-None"

        # Drain stdout/stderr for the *entire* subprocess lifetime, not just
        # during the startup handshake below. Bot output was previously only
        # read up to the first "KAI_RUN_ID=" line, then abandoned — once the
        # bot's own logging filled the OS pipe buffer (typically 64KB), its
        # next write() would block forever, hanging the bot. Forwarding every
        # line (prefixed with the instance id) also makes bot logs (message
        # received, tool calls, etc.) visible in `docker compose logs cockpit`.
        instance_id_for_logs = self._instance_id(deployment, user=user)

        def _pump_output() -> None:
            try:
                for line in iter(stdout.readline, ""):
                    line = line.rstrip("\n")
                    if not run_id_found.is_set() and "KAI_RUN_ID=" in line:
                        run_id_box.append(line.strip().split("KAI_RUN_ID=")[1].split()[0])
                        run_id_found.set()
                    if line:
                        # Print raw rather than through this logger: the bot
                        # subprocess already formats its own lines (it calls
                        # setup_logging() itself), so re-logging here would
                        # double up the timestamp/level/logger-name prefix.
                        print(f"[{instance_id_for_logs}] {line}", flush=True)
            except (ValueError, OSError):
                # Pipe closed underneath us (process killed) — fine to stop.
                pass

        pump_thread = threading.Thread(
            target=_pump_output,
            name=f"bot-output-{instance_id_for_logs}",
            daemon=True,
        )
        pump_thread.start()

        try:
            while time.time() < deadline:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                if run_id_found.wait(timeout=min(remaining, 0.5)):
                    run_id = run_id_box[0]
                    break
                if proc.poll() is not None:
                    raise DeploymentStartupError(f"Process exited with code {proc.returncode}")

            if not run_id:
                raise DeploymentStartupError("Timed out waiting for KAI_RUN_ID")

            registry = self._registry(deployment, user=user)
            deadline = time.time() + 10
            while time.time() < deadline:
                if registry.get(run_id) is not None:
                    break
                time.sleep(0.5)
            else:
                raise DeploymentStartupError(f"Run {run_id} not found in registry")
        except DeploymentStartupError:
            if hasattr(proc, "kill"):
                proc.kill()
            if hasattr(proc, "wait"):
                proc.wait(timeout=5)
            raise

        deployment.run_id = run_id
        deployment.status = "running"
        deployment.desired_state = "running"
        deployment.needs_restart = False
        deployment.updated_at = now_iso()
        self.db.commit()

    def stop(self, deployment: Deployment) -> None:
        """Stop a deployment: SIGTERM → poll → SIGKILL."""
        if not deployment.run_id:
            deployment.status = "stopped"
            deployment.desired_state = "stopped"
            deployment.needs_restart = False
            deployment.updated_at = now_iso()
            self.db.commit()
            return

        registry = self._registry(deployment)
        record = registry.get(deployment.run_id)

        if record and pid_alive(record.pid):
            # A PID can be recycled between the pid_alive check and os.kill;
            # ProcessLookupError means the process already exited on its own,
            # which is the intended end state of stop() — treat as success.
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
        deployment.updated_at = now_iso()
        self.db.commit()

    def delete(self, deployment: Deployment) -> None:
        """Delete a deployment: stop it if running, purge all per-bot state,
        then remove the DB row.

        Deleting a deployment does NOT disconnect WhatsApp — the
        account-level Connection (and its WAHA session) is left intact so
        the operator can redeploy without re-scanning the QR. Everything
        else tied to this deployment instance — config file, chat history,
        goal, runs, seen-IDs, sleep-state, and tasks — is purged so no
        orphaned state survives.
        """
        if deployment.status == "running" or deployment.run_id:
            self.stop(deployment)

        self._purge_bot_state(deployment)

        self.db.delete(deployment)
        self.db.commit()

    def _purge_bot_state(self, deployment: Deployment) -> None:
        """Remove every per-bot state file for this deployment instance.

        Unlinks the cockpit-managed config, chat history, goal, runs,
        seen-IDs, sleep-state, and tasks stores. Missing files are
        silently skipped. The WhatsApp connection and WAHA session are
        NOT touched (account-level, not deployment-level).
        """
        instance_id = self._instance_id(deployment)

        from kai.bots import load_bot
        from kai.config.settings import get_settings

        settings = get_settings()

        # agent_history_folder (history/goal/runs) is resolved by core.py
        # relative to the process CWD. tasks_folder (seen/sleep/tasks) is
        # resolved by base.py/waha's Bot relative to the *bot's own package
        # directory* instead — see kai.bots.base.Bot._resolve_store_path and
        # TaskScheduler setup. Mirror both exactly or a relative tasks_folder
        # (the default, "data") silently purges the wrong directory.
        #
        # The exact filenames below are hardcoded to match four independent
        # producers (kai.agent.core.KaiAgent._resolve_history_file for
        # history/goal, kai.runs.runs_path for runs, kai.bots.waha.Bot's
        # seen/sleep suffixes, and kai.bots.base.Bot's tasks suffix) — there
        # is no single shared naming helper. runs_path is reused directly
        # below since it's already a public function; the others have no
        # public equivalent to call. If any of those naming rules ever
        # changes, update the suffixes here too, or purge will silently
        # leave orphaned files behind (unlink uses missing_ok=True).
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

        if settings.tasks_folder is not None:
            tasks_folder = Path(settings.tasks_folder)
            if not tasks_folder.is_absolute():
                tasks_folder = load_bot(deployment.bot_type).bot_dir / tasks_folder
            for suffix in task_suffixes:
                try:
                    (tasks_folder / suffix).unlink(missing_ok=True)
                except OSError:
                    pass

        # Cockpit-managed config (may live in a different directory than
        # the state files above, e.g. data/configs/cockpit/).
        try:
            (config_writer.CONFIGS_DIR / f"{instance_id}.json").unlink(missing_ok=True)
        except OSError:
            pass

    def run_started_at(self, deployment: Deployment) -> str | None:
        """The run record's ``started_at`` ISO timestamp, or None if not running."""
        record = self._resolve_run(deployment)
        return record.started_at if record else None

    def fetch_status(self, deployment: Deployment) -> dict | None:
        """Fetch live status from the running bot, or None if stopped.

        Read-only: does not mutate the deployment.
        """
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

    def sleep_chat(self, deployment: Deployment, chat_id: str) -> dict:
        """POST to the running bot's /sleep route."""
        return self._sleep_toggle(deployment, chat_id, "sleep")

    def wake_chat(self, deployment: Deployment, chat_id: str) -> dict:
        """POST to the running bot's /wake route."""
        return self._sleep_toggle(deployment, chat_id, "wake")

    def clear_history(self, deployment: Deployment) -> dict:
        """POST to the running bot's /clear route."""
        record = self._resolve_run(deployment)
        if record is None:
            return {"ok": False, "error": "bot is not running"}

        return self._call_bot(record, "POST", "/clear")

    def history(self, deployment: Deployment) -> dict[str, list[dict]]:
        """Load the per-bot history file and return ``{chat_id: [messages]}``.

        Each message is ``{"role": str, "content": str, "ts": str | None}``.
        The ``ts`` field is the ISO-8601 UTC timestamp recorded when the
        message was stored; older history files (pre-timestamp) have
        ``None``. Returns ``{}`` when the file is missing or unreadable.
        The history file is
        written atomically by the bot process (a ``.tmp`` replace), so
        reading it from the cockpit is safe.

        The on-disk keys are namespaced as ``{instance_id}:{conversation_id}``
        (see :meth:`KaiAgent._history_key`); the returned dict strips the
        instance prefix so the cockpit surfaces the raw conversation id
        (e.g. ``operator``, ``1809...@g.us``).
        """
        instance_id = self._instance_id(deployment)

        from kai.config.settings import get_settings

        settings = get_settings()
        folder = settings.agent_history_folder
        if folder is None:
            return {}
        path = Path(folder) / f"{instance_id}.json"
        if not path.exists():
            return {}

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("failed to read history file %s", path)
            return {}
        if not isinstance(raw, dict):
            return {}

        history_data = raw.get("history", raw)
        if not isinstance(history_data, dict):
            return {}

        prefix = f"{instance_id}:"
        result: dict[str, list[dict]] = {}
        for key, messages in history_data.items():
            if not isinstance(key, str) or not isinstance(messages, list):
                continue
            chat_id = key[len(prefix) :] if key.startswith(prefix) else key
            normalized: list[dict] = []
            for m in messages:
                if isinstance(m, dict) and "role" in m and "content" in m:
                    ts = m.get("ts")
                    normalized.append(
                        {
                            "role": str(m["role"]),
                            "content": str(m["content"]),
                            "ts": ts if isinstance(ts, str) else None,
                        }
                    )
            result[chat_id] = normalized
        return result

    def interaction_summary(self, deployment: Deployment) -> tuple[int, int]:
        """Return ``(conversation_count, message_count)`` for the deployment."""
        history = self.history(deployment)
        conversation_count = len(history)
        message_count = sum(len(msgs) for msgs in history.values())
        return conversation_count, message_count

    def _sleep_toggle(self, deployment: Deployment, chat_id: str, action: str) -> dict:
        record = self._resolve_run(deployment)
        if record is None:
            return {"ok": False, "error": "bot is not running"}

        body = json.dumps({"chat_id": chat_id}).encode()
        return self._call_bot(record, "POST", f"/{action}", body)


def reconcile_deployments() -> None:
    """Restart any deployment whose ``desired_state`` is ``"running"`` but
    whose bot process isn't actually alive.

    ``status`` reflects *live* process state; ``desired_state`` persists the
    user's *intent* and is only changed by explicit start/stop actions. A
    container restart kills every spawned bot subprocess (they're children of
    the cockpit process), so without this reconciliation every previously
    running deployment would stay stopped until a human re-clicked Start.

    Call this once at cockpit startup (see ``app.py``'s startup hook). Each
    deployment is independent: a failure to restart one (e.g. WhatsApp
    disconnected) is logged and skipped rather than aborting the rest.
    """
    from kai.cockpit.db import SessionLocal

    db = SessionLocal()
    try:
        svc = DeploymentsService(db)
        deployments = db.query(Deployment).filter(Deployment.desired_state == "running").all()
        for dep in deployments:
            try:
                if svc.fetch_status(dep) is not None:
                    continue  # already alive — nothing to do
            except Exception:
                # fetch_status failed (network error, registry corruption).
                # Do NOT fall through to start(): if the bot is actually
                # running but the status check failed, spawning a second
                # subprocess would leak a duplicate bot.
                logger.exception("reconcile: fetch_status failed for deployment %s", dep.id)
                continue

            try:
                svc.start(dep)
                logger.info("reconcile: restarted deployment %s", dep.id)
            except ConnectionRequiredError:
                logger.warning("reconcile: skipping deployment %s — WhatsApp not connected", dep.id)
            except DeploymentStartupError as exc:
                logger.warning("reconcile: failed to restart deployment %s: %s", dep.id, exc)
            except Exception:
                logger.exception("reconcile: unexpected error restarting deployment %s", dep.id)
    finally:
        db.close()


def topbar_status(request, user) -> str:
    """Overall deployment health for the topbar indicator, shown on every
    page. Deliberately cheap: only reads the persisted ``status`` and
    ``desired_state`` columns (no live process probing like
    ``fetch_status``), so it's safe to call once per template render.

    Red is reserved for an actual problem — a deployment whose user-chosen
    ``desired_state`` is ``"running"`` but whose live ``status`` isn't (i.e.
    it crashed or died unexpectedly). A deployment the user intentionally
    stopped (``desired_state == "stopped"``) is *not* a problem, so a fleet
    that's entirely, deliberately stopped is "warn" (idle), not "down".

    ``user`` is the already-loaded ``User`` from the template context (every
    route renders via ``require_user`` / ``get_current_user``). A ``None``
    user (logged out, or a disabled account that ``get_current_user``
    filtered out) is treated exactly like being logged out, so no
    disabled-account deployment health leaks.

    Reuses the request-scoped DB session that ``get_db`` stashes on
    ``request.state.db`` for the deployment-status query.

    Returns one of:
      - ``"none"``  — logged out, disabled account, or zero deployments
      - ``"ok"``    — no unexpected-down deployments, and at least one running
      - ``"warn"``  — either a partial problem, or everything intentionally
                      idle (nothing running, nothing unexpectedly down)
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

    if unexpected_down == 0:
        return "ok" if running > 0 else "warn"
    return "down" if running == 0 else "warn"
