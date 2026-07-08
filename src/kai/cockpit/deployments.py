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
    """Raised when a waha deployment is started without a connected WhatsApp."""


class DeploymentStartupError(Exception):
    """Raised when the bot subprocess fails to start or register."""


def _instance_id(bot_type: str, email: str) -> str:
    """Compute the per-bot instance namespace the spawned process uses."""
    return f"{bot_type}-{email}"


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

        bt = BOT_TYPES[bot_type]
        feature_flags = {f: False for f in bt.feature_flags}

        dep = Deployment(
            user_id=user.id,
            bot_type=bot_type,
            goal=goal.strip(),
            language=language.strip(),
            voice=voice.strip(),
            settings=settings,
            feature_flags=feature_flags,
            status="needs_connect",
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
        # detail page and dashboard can show a durable "restart to apply"
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

        conn = (
            self.db.query(Connection)
            .filter(
                Connection.user_id == user.id,
                Connection.service == "whatsapp",
                Connection.status == "connected",
            )
            .first()
        )
        if not conn:
            raise ConnectionRequiredError("Connect WhatsApp first at /connections")

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

        env = {
            **os.environ,
            "KAI_WAHA_SESSION": conn.config["waha_session"],
            "KAI_WAHA_HMAC_KEY": user.hmac_key,
            "KAI_WAHA_WEBHOOK_PORT": str(conn.config["waha_webhook_port"]),
            "KAI_WAHA_WEBHOOK_HOST": "0.0.0.0",
            "KAI_WAHA_WEBHOOK_PUBLIC_HOST": os.environ.get("KAI_WAHA_WEBHOOK_PUBLIC_HOST", ""),
            "KAI_WAHA_WEBHOOK_PATH": conn.config["waha_webhook_path"],
            "KAI_WAHA_WHISPER_SERVER_HOST": os.environ.get(
                "KAI_WAHA_WHISPER_SERVER_HOST", "127.0.0.1"
            ),
            "KAI_WAHA_WHISPER_SERVER_PORT": os.environ.get("KAI_WAHA_WHISPER_SERVER_PORT", "8787"),
            "KAI_WAHA_KOKORO_SERVER_HOST": os.environ.get(
                "KAI_WAHA_KOKORO_SERVER_HOST", "127.0.0.1"
            ),
            "KAI_WAHA_KOKORO_SERVER_PORT": os.environ.get("KAI_WAHA_KOKORO_SERVER_PORT", "8788"),
            "KAI_CONFIGS_DIR": "data/configs/cockpit",
        }
        if brain_conn is not None:
            env["KAI_BRAIN_WORKSPACE"] = brain_conn.config.get("workspace", "default")
            env["KAI_BRAIN_INSTRUCTION"] = brain_conn.config.get("instruction", "")
            env["KAI_BRAIN_MANDATORY"] = "true" if brain_conn.config.get("mandatory") else "false"

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
        """Delete a deployment: stop it if running, then remove row + config.

        Deleting a deployment does NOT disconnect WhatsApp
        — the account-level Connection (and its WAHA session) is left intact
        so the operator can redeploy without re-scanning the QR. Only the
        deployment record and its cockpit-managed bot config file are
        removed; per-bot state files (tasks/sleep/seen/history) under the
        data volume are left in place (they're harmless once no process
        references them and are cheap to keep).
        """
        if deployment.status == "running" or deployment.run_id:
            self.stop(deployment)

        # Remove the cockpit-managed bot config file so a future deployment
        # of the same instance id starts from BotConfig defaults rather than
        # inheriting this one's stale overrides.
        try:
            config_path = config_writer.CONFIGS_DIR / f"{self._instance_id(deployment)}.json"
            config_path.unlink(missing_ok=True)
        except OSError:
            pass

        self.db.delete(deployment)
        self.db.commit()

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
