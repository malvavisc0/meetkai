"""Persistent set of already-processed message IDs.

Survives restarts so a webhook redelivery after a restart isn't processed
twice. Mirrors :class:`kai.agent.scheduler.TaskStore`: atomic writes
(temp file + replace), JSON on disk, failures logged not raised, and a
``path=None`` mode that degrades to in-memory-only (read-only filesystems,
tests).

Format: ``{"seen": ["id1", "id2", ...]}`` — newest at the end. Capped at
``max_size`` (LRU eviction of the oldest) so the file can't grow
unbounded on a long-running bot.

Two persistence paths share one in-memory mutation:

* :meth:`add` — synchronous write-through (direct/test use).
* :meth:`add_async` — used on the bot's hot message path. The in-memory
  update is synchronous (dedup is correct immediately), but the blocking
  file write is offloaded to a worker thread via :func:`asyncio.to_thread`
  so it never stalls the event loop. The on-disk snapshot is taken *on the
  loop* before handing off, so the worker thread never iterates the live
  ``OrderedDict`` (which the loop may mutate concurrently).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger(__name__)


class SeenStore:
    """Persistent LRU set of seen message IDs.

    In-memory mutations run on the event loop and are atomic w.r.t. other
    coroutines (no ``await`` inside them). The async write path serializes
    file writes with a lazily-created per-loop :class:`asyncio.Lock` (bound
    on first use, mirroring :class:`TaskStore`, so a store reused across
    ``asyncio.run`` calls in tests doesn't bind to a dead loop).
    """

    def __init__(self, path: Path | None, max_size: int = 2048) -> None:
        self._path = path
        self._max_size = max_size
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock: asyncio.Lock | None = None
        self._load()

    def _lock_for(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

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

    def _snapshot(self) -> dict[str, list[str]]:
        """Capture the current state for serialization. Call on the loop."""
        return {"seen": list(self._seen.keys())}

    def _write(self, data: dict[str, list[str]]) -> None:
        """Atomically write ``data`` to disk. Safe to run in a worker thread.

        Touches only ``data`` and the filesystem — never the live
        ``OrderedDict`` — so it can't race the event loop's mutations.
        """
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(f"{self._path}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            logger.warning("Failed to save seen IDs to %s: %s", self._path, exc)

    def _save(self) -> None:
        self._write(self._snapshot())

    def _add_in_memory(self, message_id: str) -> bool:
        """Update the in-memory set; return True if a persist is needed.

        Idempotent: re-adding an already-seen ID is a no-op (returns False)
        but moves it to the end so eviction stays true LRU.
        """
        if not message_id:
            return False
        if message_id in self._seen:
            self._seen.move_to_end(message_id)
            return False
        self._seen[message_id] = None
        if len(self._seen) > self._max_size:
            self._seen.popitem(last=False)
        return True

    def is_seen(self, message_id: str) -> bool:
        """Return True if ``message_id`` was already marked seen."""
        return message_id in self._seen

    def add(self, message_id: str) -> None:
        """Mark ``message_id`` as seen and persist synchronously (write-through).

        Blocks on the file write; prefer :meth:`add_async` on the event loop.
        """
        if self._add_in_memory(message_id):
            self._save()

    async def add_async(self, message_id: str) -> None:
        """Mark ``message_id`` as seen, offloading the file write to a thread.

        The in-memory update is synchronous, so :meth:`is_seen` reflects it
        immediately. Only the blocking disk write is moved off the loop.
        """
        if not self._add_in_memory(message_id):
            return
        if self._path is None:
            return
        # Snapshot on the loop, then write on a worker thread. Serialize writes
        # so concurrent calls can't race the temp-file replace.
        async with self._lock_for():
            data = self._snapshot()
            await asyncio.to_thread(self._write, data)
