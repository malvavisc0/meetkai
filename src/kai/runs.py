"""Per-instance run registry for the operator ``tell`` surface.

When a bot is launched the framework generates a short ``run_id`` and
registers it in ``<data>/<instance>.runs.json`` — a single-record file,
because one instance holds at most one live bot. ``kai bot tell`` resolves a
``run_id`` against that record to get its endpoint + HMAC key.
"""

import json
import logging
import os
import re
import secrets
from pathlib import Path

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

_INSTANCE_UNSAFE = re.compile(r"[^A-Za-z0-9._@+-]")


def generate_run_id() -> str:
    """Generate a short, copy-friendly run token (8 hex chars)."""
    return secrets.token_hex(4)


def sanitize_instance_id(instance_id: str) -> str:
    """Reduce an instance id to a single, filename-safe path component.

    Instance ids are built from operator emails and bot names, so strip path
    separators and other specials that could escape the data folder. Ordinary
    emails (``@``, ``.``, ``+``, ``-``, ``_``) are preserved; anything else
    becomes ``_``.
    """
    cleaned = _INSTANCE_UNSAFE.sub("_", instance_id)
    return cleaned or "unknown"


def pid_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` is currently running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but we may not signal it; treat as alive.
        return True
    except OSError:
        return False
    return True


def proc_start_time(pid: int) -> int | None:
    """Return a process's kernel start tick (``/proc/<pid>/stat`` field 22).

    The start tick is fixed at process creation and differs once the OS
    reuses the PID, so it identifies a process across PID reuse. Returns
    ``None`` when ``/proc`` is unavailable (non-Linux) or the process is gone.
    """
    try:
        data = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    rparen = data.rfind(b")")
    if rparen == -1:
        return None
    # Fields after comm start at field 3 (state); starttime (field 22) is index 19.
    fields = data[rparen + 1 :].split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


class RunRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    endpoint: str
    hmac_key: str
    hmac_algorithm: str = "sha512"
    pid: int
    pid_start: int | None = None
    started_at: str


def record_is_live(record: RunRecord) -> bool:
    """Return True only while ``record.pid`` is still the recorded process.

    Guards against PID reuse: when the recorded start tick differs from the
    live process, the original bot is gone and the record must not be used to
    signal whatever process now holds the PID.
    """
    if record.pid_start is not None:
        current = proc_start_time(record.pid)
        if current is not None:
            return current == record.pid_start
    return pid_alive(record.pid)


class RunRegistry:
    """JSON-backed record of the single live run for one instance."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> dict | None:
        if not self.path.is_file():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load runs file %s: %s", self.path, exc)
            return None
        return raw if isinstance(raw, dict) else None

    def save(self, record: RunRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def replace(self, record: RunRecord) -> None:
        """Register ``record`` as the instance's sole run, replacing any prior.

        Old entries from killed/crashed processes are never reliably prunable:
        PIDs get recycled by the OS, so a dead run's pid can coincidentally
        match a live process. Wipe the file down to just the new record.
        """
        self.save(record)
        logger.info(
            "Registered run %s -> %s (pid %s), replacing prior run for this instance",
            record.run_id,
            record.endpoint,
            record.pid,
        )

    def get(self) -> RunRecord | None:
        """Return the live run record, or None when there is none.

        A record that fails validation or whose process is gone (or whose PID
        was reused) is pruned: the registry only ever hands out live runs.
        Old multi-run files from earlier versions also fail validation here
        and self-heal away.
        """
        raw = self.load()
        if raw is None:
            return None
        try:
            record = RunRecord.model_validate(raw)
        except Exception as exc:
            logger.warning("Invalid run record in %s: %s", self.path, exc)
            self.remove()
            return None
        if not record_is_live(record):
            logger.info("Pruned stale run %s (pid %s dead or reused)", record.run_id, record.pid)
            self.remove()
            return None
        return record

    def remove(self) -> None:
        self.path.unlink(missing_ok=True)


def runs_path(data_folder: Path | str | None, bot_name: str) -> Path:
    """Resolve the per-instance runs file path (instance id sanitized)."""
    folder = Path(data_folder) if data_folder else Path("data")
    return folder / f"{sanitize_instance_id(bot_name)}.runs.json"
