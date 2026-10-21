import asyncio
import hashlib
import json
import logging
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from llama_index.core.llms import ChatMessage, MessageRole

from kai.config.settings import Settings

FLUSH_DELAY = 0.5
VALID_ROLES = frozenset(r.value for r in MessageRole)

logger = logging.getLogger(__name__)


class HistoryStoreMixin:
    """History persistence and cache management mixed into ``KaiAgent``.

    Methods operate on the agent's ``_history``/``_timestamps`` attributes so
    direct access (including tests and the conversation tools) is unchanged.
    """

    settings: Settings
    _namespace: str
    _history: OrderedDict[str, list[ChatMessage]]
    _timestamps: OrderedDict[str, list[str | None]]
    _history_file: Path | None
    _history_dir: Path | None
    _max_history: int
    _max_history_chars: int
    _max_conversations: int
    _save_lock: asyncio.Lock
    _write_lock: asyncio.Lock
    _dirty: bool
    _dirty_keys: set[str]
    _flush_task: asyncio.Task | None

    def resolve_history_file(self) -> Path | None:
        """Return the legacy single-file path, or None when persistence is off."""
        folder = self.settings.agent_history_folder
        if folder is None:
            return None
        name = self._namespace or "default"
        return Path(folder) / f"{name}.json"

    def resolve_history_dir(self) -> Path | None:
        """Return this agent's per-conversation history directory."""
        folder = self.settings.agent_history_folder
        if folder is None:
            return None
        name = self._namespace or "default"
        return Path(folder) / f"{name}.history"

    @staticmethod
    def conversation_filename(key: str) -> str:
        """Return a filesystem-safe filename for a conversation key."""
        return f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}.json"

    @staticmethod
    def now_ts() -> str:
        """ISO-8601 UTC timestamp for a history message."""
        return datetime.now(UTC).isoformat()

    def history_key(self, conversation_id: str | None = None) -> str:
        """Return the namespaced history key for a conversation."""
        if not conversation_id:
            logger.debug("conversation_id not provided; using 'default' bucket")
            key = "default"
        else:
            key = conversation_id
        if self._namespace:
            return f"{self._namespace}:{key}"
        return key

    def load_history_state(self) -> None:
        """Populate ``_history`` and ``_timestamps`` from disk.

        Reads the per-conversation directory when present, falling back to
        the legacy single-file layout. Timestamps are optional: a missing or
        non-string ``ts`` field is recorded as ``None`` so the parallel-list
        invariant (one timestamp entry per message) is preserved across loads
        of old files.
        """
        self._history.clear()
        self._timestamps.clear()
        if self._history_dir is None:
            return

        if self._history_dir.is_dir():
            self.load_conversation_dir()
        # Merge the legacy file even when the directory exists: a crash during
        # migration leaves both formats, and the directory may only hold the
        # conversations written before the crash.
        self.load_legacy_history_file()
        self.evict_excess_conversations()

    def load_conversation_dir(self) -> bool:
        """Load conversations from the per-conversation directory."""
        if self._history_dir is None:
            return False
        try:
            paths = sorted(
                self._history_dir.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
            )
        except OSError as exc:
            logger.warning("Failed to list agent history in %s: %s", self._history_dir, exc)
            return False

        loaded = False
        for path in self.cap_history_paths(paths):
            parsed = self.read_conversation_file(path)
            if parsed is None:
                continue
            key, messages = parsed
            self._history[key], self._timestamps[key] = messages
            loaded = True
        return loaded

    def cap_history_paths(self, paths: list[Path]) -> list[Path]:
        """Delete the oldest files beyond the conversation cap."""
        if len(paths) <= self._max_conversations:
            return paths
        keep = paths[-self._max_conversations :] if self._max_conversations > 0 else []
        for path in paths[: len(paths) - len(keep)]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.debug("Failed to delete stale history file %s", path)
        return keep

    def read_conversation_file(
        self, path: Path
    ) -> tuple[str, tuple[list[ChatMessage], list[str | None]]] | None:
        """Parse one conversation file, returning None when it is invalid."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Skipping unreadable history file %s: %s", path, exc)
            return None
        if not isinstance(raw, dict):
            return None
        key = raw.get("key")
        messages = raw.get("messages")
        if not isinstance(key, str) or not isinstance(messages, list):
            return None
        return key, self.parse_history_messages(messages)

    def load_legacy_history_file(self) -> None:
        """Load the legacy single-file history layout."""
        if self._history_file is None:
            return
        try:
            if not self._history_file.exists():
                return
            raw = json.loads(self._history_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                logger.warning("Ignoring invalid agent history file: %s", self._history_file)
                return

            history_data = raw.get("history", raw)
            if not isinstance(history_data, dict):
                return

            for key, messages in history_data.items():
                if not isinstance(key, str) or not isinstance(messages, list):
                    continue
                if key in self._history:
                    continue
                self._history[key], self._timestamps[key] = self.parse_history_messages(messages)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Failed to load agent history from %s: %s", self._history_file, exc)
            self._history.clear()
            self._timestamps.clear()

    @staticmethod
    def parse_history_messages(messages: list) -> tuple[list[ChatMessage], list[str | None]]:
        """Parse persisted message dicts into chat messages and timestamps."""
        msgs: list[ChatMessage] = []
        ts_list: list[str | None] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content")
            if role in VALID_ROLES and isinstance(content, str):
                msgs.append(ChatMessage(role=MessageRole(role), content=content))
                ts = item.get("ts")
                ts_list.append(ts if isinstance(ts, str) else None)
        return msgs, ts_list

    def save_history(self) -> None:
        """Persist the current history snapshot."""
        if self._history_file is None:
            return
        self.save_from_snapshot(self._history, self._timestamps)

    @staticmethod
    def serialize_history_message(message: ChatMessage, ts: str | None) -> dict:
        """Serialize one history message, including its optional timestamp."""
        item = {"role": message.role.value, "content": message.content or ""}
        if ts:
            item["ts"] = ts
        return item

    def get_history(self, conversation_id: str | None = None) -> list[ChatMessage]:
        """Return the message list for a conversation, creating it if needed."""
        key = self.history_key(conversation_id)
        if key in self._history:
            self._history.move_to_end(key)
            # Self-heal a parallel-list divergence (e.g. a test or external
            # caller mutating _history directly): ensure _timestamps has a
            # matching entry, backfilling with None when it doesn't.
            if key not in self._timestamps:
                self._timestamps[key] = [None] * len(self._history[key])
            else:
                self._timestamps.move_to_end(key)
        else:
            self._history[key] = []
            self._timestamps[key] = []
            self.evict_excess_conversations()
        return self._history[key]

    def evict_excess_conversations(self) -> None:
        """Drop the oldest conversations beyond the cap and delete their files."""
        while len(self._history) > self._max_conversations:
            evicted_key, _ = self._history.popitem(last=False)
            self._timestamps.pop(evicted_key, None)
            if self._history_dir is not None:
                path = self._history_dir / self.conversation_filename(evicted_key)
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.debug("Failed to delete evicted history file %s", path)
            logger.debug("Evicted conversation from history cache: %s", evicted_key)

    def history_within_limits(self, history: list[ChatMessage]) -> bool:
        """Return True when history fits both the message and character limits."""
        if len(history) > self._max_history:
            return False
        return sum(len(message.content or "") for message in history) <= self._max_history_chars

    def trim_to_char_budget(
        self, history: list[ChatMessage], ts_list: list[str | None]
    ) -> tuple[list[ChatMessage], list[str | None]]:
        """Keep the newest messages that fit the character budget."""
        tail = history[-self._max_history :] if self._max_history > 0 else history
        base = len(history) - len(tail)
        trimmed: list[ChatMessage] = []
        kept_idx: list[int] = []
        total_chars = 0
        for j, message in enumerate(reversed(tail)):
            content_length = len(message.content or "")
            if trimmed and total_chars + content_length > self._max_history_chars:
                break
            trimmed.append(message)
            kept_idx.append(base + len(tail) - 1 - j)
            total_chars += content_length
        kept_idx.reverse()
        timestamps = [ts_list[i] if i < len(ts_list) else None for i in kept_idx]
        return list(reversed(trimmed)), timestamps

    def trim_history(self, conversation_id: str | None = None) -> None:
        """Trim a conversation to the configured message and character limits."""
        key = self.history_key(conversation_id)
        history = self._history.get(key, [])
        ts_list = self._timestamps.get(key, [])
        if self._max_history == 0 or self._max_history_chars == 0:
            self._history[key] = []
            self._timestamps[key] = []
            return
        if self.history_within_limits(history):
            self._history.setdefault(key, [])
            self._timestamps[key] = [
                ts_list[i] if i < len(ts_list) else None for i in range(len(history))
            ]
            return
        self._history[key], self._timestamps[key] = self.trim_to_char_budget(history, ts_list)

    async def clear_history(self, conversation_id: str | None = None) -> None:
        """Clear a single conversation bucket's history (e.g. a ``/clear`` route).

        Mutates ``_history`` under ``_save_lock`` — like every other mutator
        (``observe``, ``record_assistant_message``, ``chat``) — so it can't
        race with a concurrent ``flush_now`` snapshot.
        """
        key = self.history_key(conversation_id)
        async with self._save_lock:
            self._history[key] = []
            self._timestamps[key] = []
        self.mark_dirty(key)

    def get_conversation_history(self, conversation_id: str | None = None) -> list[ChatMessage]:
        """Return the stored messages for a conversation (read-only).

        Public read counterpart to :meth:`record_assistant_message` and
        :meth:`observe`. Used by the ``get_conversation_messages`` tool so the
        operator (or the model on an autonomous turn) can recall what was said
        in a conversation other than the one the current turn is running under.
        """
        return list(self.get_history(conversation_id))

    def list_conversations(self) -> list[tuple[str, int, str | None]]:
        """Return known, non-empty conversations as ``(id, message_count, last_ts)``.

        Read-only — never creates entries or triggers eviction. IDs are
        stripped of the agent's namespace prefix. Ordered most-recently-active
        first. Used by the ``list_conversations`` tool so the model can
        discover conversation_ids to pass to ``get_conversation_messages``.
        """
        prefix = f"{self._namespace}:" if self._namespace else ""
        results: list[tuple[str, int, str | None]] = []
        for key, messages in self._history.items():
            if not messages:
                continue
            cid = key[len(prefix) :] if prefix and key.startswith(prefix) else key
            ts_list = self._timestamps.get(key) or []
            last_ts = ts_list[-1] if ts_list else None
            results.append((cid, len(messages), last_ts))
        results.reverse()
        return results

    def write_conversation_file(
        self,
        key: str,
        messages: list[ChatMessage],
        timestamps: dict[str, list[str | None]],
    ) -> str | None:
        """Write one conversation file; return its filename, or None if removed."""
        if self._history_dir is None:
            return None
        path = self._history_dir / self.conversation_filename(key)
        if not messages:
            path.unlink(missing_ok=True)
            return None
        ts_list = timestamps.get(key, [])
        items = [
            self.serialize_history_message(message, ts_list[i] if i < len(ts_list) else None)
            for i, message in enumerate(messages)
        ]
        temp_path = Path(f"{path}.{uuid4().hex}.tmp")
        temp_path.write_text(
            json.dumps({"key": key, "messages": items}, ensure_ascii=False),
            encoding="utf-8",
        )
        temp_path.replace(path)
        return path.name

    def prune_conversation_files(self, written: set[str]) -> None:
        """Delete conversation files not in the latest full snapshot."""
        if self._history_dir is None:
            return
        for path in self._history_dir.glob("*.json"):
            if path.name not in written:
                path.unlink(missing_ok=True)

    def save_from_snapshot(
        self,
        snapshot: dict[str, list[ChatMessage]],
        timestamps: dict[str, list[str | None]] | None = None,
        *,
        replace: bool = True,
    ) -> bool:
        """Write conversations to the per-conversation history directory.

        ``replace`` also removes files for conversations absent from the
        snapshot; partial flushes pass ``replace=False`` and only rewrite
        the dirty conversations. Returns False when the write failed.
        """
        if self._history_dir is None:
            return False
        if timestamps is None:
            timestamps = self._timestamps
        try:
            self._history_dir.mkdir(parents=True, exist_ok=True)
            written: set[str] = set()
            for key, messages in snapshot.items():
                name = self.write_conversation_file(key, messages, timestamps)
                if name is not None:
                    written.add(name)
            if replace:
                self.prune_conversation_files(written)
            if self._history_file is not None:
                self._history_file.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to save agent history to %s: %s", self._history_dir, exc)
            return False
        return True

    def mark_dirty(self, key: str | None = None) -> None:
        """Mark history as modified and schedule a debounced flush."""
        if self._history_dir is None:
            return
        self._dirty = True
        if key is None:
            self._dirty_keys.update(self._history)
        else:
            self._dirty_keys.add(key)
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.ensure_future(self.flush_after_delay())

    async def flush_after_delay(self) -> None:
        """Flush history after the debounce delay."""
        try:
            await asyncio.sleep(FLUSH_DELAY)
            await self.flush_now()
        except asyncio.CancelledError:
            await self.flush_now()

    async def flush_now(self) -> None:
        """Flush pending history changes, serializing concurrent writers."""
        async with self._write_lock:
            await self.flush_once()

    async def flush_once(self) -> None:
        """Take a snapshot and write pending history changes when dirty."""
        async with self._save_lock:
            plan = self.snapshot_for_flush()
        if plan is None:
            return
        snapshot, ts_snapshot, keys, migrating = plan
        saved = await asyncio.to_thread(
            self.save_from_snapshot, snapshot, ts_snapshot, replace=migrating
        )
        async with self._save_lock:
            if saved:
                for key in keys:
                    self._dirty_keys.discard(key)
                self._dirty = bool(self._dirty_keys)
            else:
                self._dirty_keys.update(keys)
                self._dirty = True
        if saved and self._dirty:
            self._flush_task = asyncio.ensure_future(self.flush_after_delay())

    def snapshot_for_flush(
        self,
    ) -> tuple[dict[str, list[ChatMessage]], dict[str, list[str | None]], list[str], bool] | None:
        """Capture the dirty conversations to write; None when there is nothing."""
        if not self._dirty:
            return None
        migrating = self._history_file is not None and self._history_file.exists()
        keys = list(self._history) if migrating else list(self._dirty_keys)
        if not keys and not migrating:
            self._dirty = False
            return None
        snapshot = {k: list(self._history.get(k, [])) for k in keys}
        ts_snapshot = {k: list(self._timestamps.get(k, [])) for k in keys}
        return snapshot, ts_snapshot, keys, migrating

    async def flush(self) -> None:
        """Persist any pending history changes immediately."""
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        await self.flush_now()
