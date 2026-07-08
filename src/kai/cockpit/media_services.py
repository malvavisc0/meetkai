"""Cockpit-owned lifecycle for shared whisper-server + kokoro server.

``MediaServiceManager`` spawns both services once in the reconcile background
thread, blocks until every *enabled* service reports healthy, and sets the
module-level ``MEDIA_READY`` threading event.  Bot-spawn paths gate on it
so no bot launches before STT/TTS are up.

Bots are pure HTTP clients (health probe + /inference or /synthesize).
"""

from __future__ import annotations

import logging
import signal
import subprocess
import threading
import time
from pathlib import Path

import httpx

from kai.bots.waha.config import WahaSettings
from kai.vendors.manager import VendorManager

logger = logging.getLogger(__name__)

MEDIA_READY = threading.Event()

_HEALTH_POLL_INTERVAL = 1.0
_HEALTH_POLL_ATTEMPTS = 60


class MediaServiceManager:
    """Cockpit-owned lifecycle for the shared whisper-server + kokoro server.

    Started once in the reconcile background thread (before ``reconcile_deployments``);
    stopped on cockpit shutdown.  Bots are pure clients (health probe only).
    """

    def __init__(self, settings: WahaSettings, vendor_manager: VendorManager) -> None:
        self._settings = settings
        self._vendors = vendor_manager
        self._whisper_proc: subprocess.Popen | None = None
        self._kokoro_proc: subprocess.Popen | None = None

    def start_all(self) -> None:
        """Spawn both services, block until healthy, set ``MEDIA_READY``.

        Idempotent: probes ``/health`` first; if already running, reuses.
        A service intentionally disabled by config is not required for readiness.
        """
        whisper_ok = self._start_whisper()
        kokoro_ok = self._start_kokoro()

        if whisper_ok and kokoro_ok:
            MEDIA_READY.set()
            logger.info("all media services ready")
        else:
            parts = []
            if not whisper_ok:
                parts.append("whisper")
            if not kokoro_ok:
                parts.append("kokoro")
            logger.warning("media services not fully ready; missing: %s", ", ".join(parts))

    def stop_all(self) -> None:
        """SIGTERM both, then SIGKILL on timeout, then reap.

        Only stops processes this manager spawned (not a pre-existing one it
        reused).
        """
        for name, proc in [
            ("whisper", self._whisper_proc),
            ("kokoro", self._kokoro_proc),
        ]:
            if proc is None:
                continue
            if proc.poll() is not None:
                logger.info("%s already exited (rc=%d)", name, proc.returncode)
                continue
            logger.info("stopping %s (pid=%d)", name, proc.pid)
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.warning("%s did not exit; sending SIGKILL", name)
                proc.kill()
                proc.wait()
            logger.info("%s stopped", name)
        self._whisper_proc = None
        self._kokoro_proc = None

    def wait_ready(self, timeout: float) -> bool:
        """Block until ``MEDIA_READY`` is set or *timeout* elapses.

        Returns ``True`` if ready, ``False`` on timeout.
        """
        return MEDIA_READY.wait(timeout=timeout)

    def status(self) -> dict:
        """Return a status dict for diagnostics."""
        return {
            "whisper": {
                "running": self._whisper_proc is not None and self._whisper_proc.poll() is None,
                "pid": self._whisper_proc.pid if self._whisper_proc else None,
            },
            "kokoro": {
                "running": self._kokoro_proc is not None and self._kokoro_proc.poll() is None,
                "pid": self._kokoro_proc.pid if self._kokoro_proc else None,
            },
            "ready": MEDIA_READY.is_set(),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _probe_health(self, url: str) -> bool:
        """Single-shot GET /health probe.  Returns True on 200."""
        try:
            resp = httpx.get(url, timeout=2)
            return resp.status_code == 200
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.HTTPError):
            return False

    def _resolve_path(self, path_str: str) -> Path | None:
        """Resolve *path_str*, trying it relative to the project root as a
        fallback.  Returns ``None`` if neither location has the file.
        """
        path = Path(path_str)
        if path.is_file():
            return path
        path = self._vendors.project_root / path
        return path if path.is_file() else None

    def _spawn_and_wait_healthy(
        self, name: str, argv: list[str], health_url: str
    ) -> subprocess.Popen | None:
        """Spawn *argv* and poll *health_url* until it responds or the
        process exits/times out.  Returns the running ``Popen``, or ``None``
        on failure (already logged).
        """
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        for _attempt in range(_HEALTH_POLL_ATTEMPTS):
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode() if proc.stderr else ""
                logger.error("%s exited during startup: %s", name, stderr)
                return None
            if self._probe_health(health_url):
                logger.info("%s ready (pid=%d)", name, proc.pid)
                return proc
            time.sleep(_HEALTH_POLL_INTERVAL)

        logger.error("%s did not become ready within %ds", name, _HEALTH_POLL_ATTEMPTS)
        proc.kill()
        proc.wait()
        return None

    def _start_whisper(self) -> bool:
        """Spawn or reuse the whisper-server.  Returns True when healthy."""
        settings = self._settings
        host = settings.whisper_server_host
        port = settings.whisper_server_port
        health_url = f"http://{host}:{port}/health"

        # Already running (e.g. operator ran it manually)?
        if self._probe_health(health_url):
            logger.info("whisper-server already healthy at %s:%d", host, port)
            return True

        whisper_vendor = self._vendors.get("whisper")
        if not whisper_vendor.is_installed():
            logger.warning("whisper vendor not installed; skipping whisper-server")
            return False

        server_bin = whisper_vendor.vendor_dir / "whisper-server"
        if not server_bin.is_file():
            logger.warning("whisper-server binary not found at %s", server_bin)
            return False

        model_path = self._resolve_path(settings.whisper_model_path)
        if model_path is None:
            logger.warning("whisper model not found at %s", settings.whisper_model_path)
            return False

        logger.info(
            "starting whisper-server on %s:%d (model=%s, lang=auto)", host, port, model_path
        )
        self._whisper_proc = self._spawn_and_wait_healthy(
            "whisper-server",
            [
                str(server_bin),
                "-m",
                str(model_path),
                "-l",
                "auto",
                "-t",
                str(settings.whisper_server_threads),
                "--host",
                host,
                "--port",
                str(port),
            ],
            health_url,
        )
        return self._whisper_proc is not None

    def _start_kokoro(self) -> bool:
        """Spawn or reuse the kokoro server.  Returns True when healthy."""
        settings = self._settings
        if not settings.kokoro_enabled:
            logger.info("kokoro disabled by config; skipping")
            return True

        host = settings.kokoro_server_host
        port = settings.kokoro_server_port
        health_url = f"http://{host}:{port}/health"

        # Already running?
        if self._probe_health(health_url):
            logger.info("kokoro server already healthy at %s:%d", host, port)
            return True

        kokoro_vendor = self._vendors.get("kokoro")
        if not kokoro_vendor.is_installed():
            logger.warning("kokoro vendor not installed; skipping kokoro server")
            return False

        venv_python = kokoro_vendor.vendor_dir / "bin" / "python"
        if not venv_python.is_file():
            logger.warning("kokoro venv python not found at %s", venv_python)
            return False

        model_path = self._resolve_path(settings.kokoro_model_path)
        if model_path is None:
            logger.warning("kokoro model not found at %s", settings.kokoro_model_path)
            return False

        voices_path = self._resolve_path(settings.kokoro_voices_path)
        if voices_path is None:
            logger.warning("kokoro voices not found at %s", settings.kokoro_voices_path)
            return False

        server_script = Path(__file__).resolve().parents[1] / "media" / "kokoro_server.py"

        logger.info("starting kokoro server on %s:%d", host, port)
        self._kokoro_proc = self._spawn_and_wait_healthy(
            "kokoro server",
            [
                str(venv_python),
                str(server_script),
                "--model",
                str(model_path),
                "--voices",
                str(voices_path),
                "--host",
                host,
                "--port",
                str(port),
            ],
            health_url,
        )
        return self._kokoro_proc is not None
