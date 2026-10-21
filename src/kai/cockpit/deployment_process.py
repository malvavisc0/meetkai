"""Process and run control for bot deployments."""

import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from kai.cockpit import config_writer
from kai.cockpit.deployment_helpers import DeploymentStartupError
from kai.cockpit.models import Deployment, User
from kai.runs import record_is_live, runs_path
from kai.utils.common import now_iso

if TYPE_CHECKING:
    from kai.cockpit.deployments import DeploymentsService

logger = logging.getLogger(__name__)


def spawn_and_await_run_id(
    svc: "DeploymentsService",
    deployment: Deployment,
    user: User,
    argv: list[str],
    env: dict[str, str],
) -> str:
    """Spawn the bot, drain its output, and wait for its run id."""
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
    instance_id_for_logs = svc.deployment_instance_id(deployment, user=user)

    def pump_output() -> None:
        """Drain bot stdout, capture the run id, and echo lines to the log."""
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
        target=pump_output,
        name=f"bot-output-{instance_id_for_logs}",
        daemon=True,
    )
    pump_thread.start()

    try:
        run_id = svc.wait_for_run_id(proc, run_id_found, run_id_box, deadline)
        svc.await_registry_entry(deployment, user, run_id)
    except DeploymentStartupError:
        if hasattr(proc, "kill"):
            proc.kill()
        if hasattr(proc, "wait"):
            proc.wait(timeout=5)
        raise
    return run_id


def wait_for_run_id(
    proc: subprocess.Popen,
    run_id_found: threading.Event,
    run_id_box: list[str],
    deadline: float,
) -> str:
    """Block until the subprocess prints ``KAI_RUN_ID`` or fails."""
    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        if run_id_found.wait(timeout=min(remaining, 0.5)):
            return run_id_box[0]
        if proc.poll() is not None:
            raise DeploymentStartupError(f"Process exited with code {proc.returncode}")

    raise DeploymentStartupError("Timed out waiting for KAI_RUN_ID")


def await_registry_entry(
    svc: "DeploymentsService", deployment: Deployment, user: User, run_id: str
) -> None:
    """Wait for the spawned run to appear in the run registry."""
    registry = svc.run_registry(deployment, user=user)
    deadline = time.time() + 10
    while time.time() < deadline:
        record = registry.get()
        if record is not None and record.run_id == run_id:
            return
        time.sleep(0.5)
    raise DeploymentStartupError(f"Run {run_id} not found in registry")


def clear_control_port(deployment: Deployment) -> None:
    """Drop the allocated control port from deployment settings."""
    if "control_port" in deployment.settings:
        deployment.settings = {k: v for k, v in deployment.settings.items() if k != "control_port"}


def stop(
    svc: "DeploymentsService",
    deployment: Deployment,
    *,
    kill: Callable[[int, int], None] = os.kill,
) -> None:
    """Stop the bot process and mark the deployment stopped."""
    if not deployment.run_id:
        deployment.status = "stopped"
        deployment.desired_state = "stopped"
        deployment.needs_restart = False
        svc.clear_control_port(deployment)
        deployment.updated_at = now_iso()
        svc.db.commit()
        return

    record = svc.resolve_run(deployment)

    # A None record was already pruned (dead process) — or belongs to a
    # different run_id (e.g. a manually started bot on this instance),
    # whose registry entry must not be touched. Only a record this
    # deployment owns is stopped and removed.
    if record is not None:
        registry = svc.run_registry(deployment)
        try:
            kill(record.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        for _ in range(20):
            if not record_is_live(record):
                break
            time.sleep(0.5)
        else:
            if record_is_live(record):
                try:
                    kill(record.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                time.sleep(0.5)
        registry.remove()
    deployment.run_id = None
    deployment.status = "stopped"
    deployment.desired_state = "stopped"
    deployment.needs_restart = False
    svc.clear_control_port(deployment)
    deployment.updated_at = now_iso()
    svc.db.commit()


def delete(svc: "DeploymentsService", deployment: Deployment) -> None:
    """Delete a deployment: stop if running, purge per-bot state, remove row."""
    if deployment.status == "running" or deployment.run_id:
        svc.stop(deployment)

    svc.purge_bot_state(deployment)

    svc.db.delete(deployment)
    svc.db.commit()


def purge_bot_state(svc: "DeploymentsService", deployment: Deployment) -> None:
    """Remove every per-bot state file for this deployment instance."""
    instance_id = svc.deployment_instance_id(deployment)

    from kai.config.settings import get_settings

    settings = get_settings()

    # The suffixes below are hardcoded to match independent producers with no
    # shared naming helper: HistoryStoreMixin.resolve_history_file
    # (history/goal, src/kai/agent/history_store.py),
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


def run_started_at(svc: "DeploymentsService", deployment: Deployment) -> str | None:
    """Return the live run's start timestamp, if any."""
    record = svc.resolve_run(deployment)
    return record.started_at if record else None


def fetch_status(svc: "DeploymentsService", deployment: Deployment) -> dict | None:
    """Fetch live status from the running bot, or None if stopped."""
    record = svc.resolve_run(deployment)
    if record is None:
        return None

    result = svc.call_bot(record, "GET", "/status", timeout=10.0)
    if "error" in result and not result.get("ok", True):
        return None
    return result
