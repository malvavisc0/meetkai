"""Persistent set of already-processed message IDs.

Survives restarts so a webhook redelivery after a restart isn't processed
twice. Mirrors :class:`kai.agent.scheduler.TaskStore`: atomic writes
(temp file + replace), JSON on disk, failures logged not raised, and a
``path=None`` mode that degrades to in-memory-only (read-only filesystems,
tests).

Format: ``{"seen": ["id1", "id2", ...]}`` — newest at the end. Capped at
``max_size`` (LRU eviction of the oldest) so the file can't grow
unbounded on a long-running bot.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger(__name__)


class SeenStore:
    """Persistent LRU set of seen message IDs.

    Not thread-safe / not async-aware: the WAHA bot processes a chat under
    a per-chat lock and reads ``_is_seen_message`` before that lock, so the
    only concurrency here is across chats on one event loop — and writes are
    synchronous and short. If that ever changes, add an ``asyncio.Lock``
    lazily like :class:`TaskStore` does.
    """

    def __init__(self, path: Path | None, max_size: int = 2048) -> None:
        self._path = path
        self._max_size = max_size
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            ids = raw.get("seen", [])
            if not isinstance(ids, list):
                return
            for item in ids:
                if isinstance(item, str) and item:
                    # _save() wrote oldest-first, so appending preserves order.
                    self._seen[item] = None
                    if len(self._seen) > self._max_size:
                        self._seen.popitem(last=False)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load seen IDs from %s: %s", self._path, exc)

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {"seen": list(self._seen.keys())}
            tmp = Path(f"{self._path}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            logger.warning("Failed to save seen IDs to %s: %s", self._path, exc)

    def is_seen(self, message_id: str) -> bool:
        """Return True if ``message_id`` was already marked seen."""
        return message_id in self._seen

    def add(self, message_id: str) -> None:
        """Mark ``message_id`` as seen and persist (write-through).

        Idempotent: re-adding an already-seen ID is a no-op (no rewrite).
        Moves an existing ID to the end so eviction is true LRU.
        """
        if not message_id:
            return
        already = message_id in self._seen
        if already:
            self._seen.move_to_end(message_id)
            return
        self._seen[message_id] = None
        if len(self._seen) > self._max_size:
            self._seen.popitem(last=False)
        self._save()
