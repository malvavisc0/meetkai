"""Per-bot run registry for the operator ``tell`` surface.

When a bot is launched the framework generates a short ``run_id`` and
registers it in ``<data>/<bot>.runs.json``. ``kai tell`` resolves a
``run_id`` to its endpoint + HMAC key.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


def generate_run_id() -> str:
    """Generate a short, copy-friendly run token (8 hex chars)."""
    return secrets.token_hex(4)


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


class RunRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    endpoint: str
    hmac_key: str
    hmac_algorithm: str = "sha512"
    pid: int
    started_at: str
    status: str = "running"


class RunRegistry:
    """JSON-backed map of ``run_id`` -> :class:`RunRecord` for one bot."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, dict]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load runs file %s: %s", self.path, exc)
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def _save(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def replace(self, run_id: str, record: RunRecord) -> None:
        """Register ``run_id`` as the sole run for this file, discarding all others.

        Each ``<bot>-<user>.runs.json`` tracks exactly one live instance.
        Old entries from killed/crashed processes are never reliably prunable:
        PIDs get recycled by the OS, so a dead run's pid can coincidentally
        match a live process. Wipe the file down to just the new record.
        """
        self._save({run_id: record.model_dump()})
        logger.info(
            "Registered run %s -> %s (pid %s), replacing prior runs for this instance",
            run_id,
            record.endpoint,
            record.pid,
        )

    def get(self, run_id: str) -> RunRecord | None:
        data = self._load().get(run_id)
        if data is None:
            return None
        try:
            return RunRecord.model_validate(data)
        except Exception as exc:
            logger.warning("Invalid run record for %s: %s", run_id, exc)
            return None

    def remove(self, run_id: str) -> None:
        data = self._load()
        if run_id in data:
            data.pop(run_id)
            self._save(data)

    def active(self) -> dict[str, RunRecord]:
        """Return live runs, pruning entries whose pid is no longer alive."""
        data = self._load()
        live: dict[str, RunRecord] = {}
        changed = False
        for run_id, raw in list(data.items()):
            try:
                record = RunRecord.model_validate(raw)
            except Exception:
                changed = True
                data.pop(run_id, None)
                continue
            if not pid_alive(record.pid):
                changed = True
                data.pop(run_id, None)
                logger.info("Pruned stale run %s (pid %s dead)", run_id, record.pid)
                continue
            live[run_id] = record
        if changed:
            self._save(data)
        return live


def runs_path(data_folder: Path | str | None, bot_name: str) -> Path:
    """Resolve the per-bot runs file path."""
    folder = Path(data_folder) if data_folder else Path("data")
    return folder / f"{bot_name}.runs.json"
