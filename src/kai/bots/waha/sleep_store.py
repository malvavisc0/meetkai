"""Persistent set of chat IDs where the bot is asleep.

Survives restarts so a chat put to sleep stays asleep after the bot
restarts. Mirrors :class:`kai.bots.waha.seen_store.SeenStore` and
:class:`kai.agent.scheduler.TaskStore`: atomic writes (temp file +
replace), JSON on disk, failures logged not raised, and a ``path=None``
mode that degrades to in-memory-only.

Format: ``{"sleeping": ["chat1@g.us", ...]}``. Sleep is binary, so only
asleep chats are stored (absence == awake) — the file stays tiny.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class SleepStore:
    """Persistent set of chats where the bot is sleeping.

    Not thread-safe / not async-aware: sleep transitions are rare (only on
    ``<<sleep>>`` / wake) and happen under the per-chat lock. Writes are
    synchronous and short.
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._sleeping: set[str] = set()
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            ids = raw.get("sleeping", [])
            if not isinstance(ids, list):
                return
            self._sleeping = {s for s in ids if isinstance(s, str) and s}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load sleep state from %s: %s", self._path, exc)

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {"sleeping": sorted(self._sleeping)}
            tmp = Path(f"{self._path}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            logger.warning("Failed to save sleep state to %s: %s", self._path, exc)

    def is_sleeping(self, chat_id: str) -> bool:
        """Return True if ``chat_id`` is currently asleep."""
        return chat_id in self._sleeping

    def set(self, chat_id: str, sleeping: bool) -> None:
        """Mark ``chat_id`` as asleep (or awake) and persist.

        Idempotent: setting an already-correct state is a no-op (no rewrite).
        """
        if not chat_id:
            return
        already = chat_id in self._sleeping
        if already == sleeping:
            return
        if sleeping:
            self._sleeping.add(chat_id)
        else:
            self._sleeping.discard(chat_id)
        self._save()

    def all(self) -> set[str]:
        """Return a copy of all currently-sleeping chat IDs."""
        return set(self._sleeping)
