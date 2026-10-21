import asyncio
import json
import logging
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path

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
    _max_history: int
    _max_history_chars: int
    _max_conversations: int
    _save_lock: asyncio.Lock
    _dirty: bool
    _flush_task: asyncio.Task | None

    def resolve_history_file(self) -> Path | None:
        """Return this agent's history file path, or None when persistence is off."""
        folder = self.settings.agent_history_folder
        if folder is None:
            return None
        name = self._namespace or "default"
        return Path(folder) / f"{name}.json"

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
        """Populate ``_history`` and ``_timestamps`` from the history file.

        Timestamps are optional: a missing or non-string ``ts`` field is
        recorded as ``None`` so the parallel-list invariant (one timestamp
        entry per message) is preserved across loads of old files.
        """
        self._history.clear()
        self._timestamps.clear()
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
            while len(self._history) > self._max_conversations:
                evicted_key, _ = self._history.popitem(last=False)
                self._timestamps.pop(evicted_key, None)
                logger.debug("Evicted conversation from history cache: %s", evicted_key)
        return self._history[key]

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
        self.mark_dirty()

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

    def save_from_snapshot(
        self,
        snapshot: dict[str, list[ChatMessage]],
        timestamps: dict[str, list[str | None]] | None = None,
    ) -> None:
        """Write a history snapshot to disk."""
        if self._history_file is None:
            return
        if timestamps is None:
            timestamps = self._timestamps
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            serialized: dict[str, list[dict]] = {}
            for key, messages in snapshot.items():
                ts_list = timestamps.get(key, [])
                items = []
                for i, message in enumerate(messages):
                    ts = ts_list[i] if i < len(ts_list) else None
                    items.append(self.serialize_history_message(message, ts))
                serialized[key] = items
            data = {"history": serialized}
            temp_path = Path(f"{self._history_file}.tmp")
            temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(self._history_file)
        except OSError as exc:
            logger.warning("Failed to save agent history to %s: %s", self._history_file, exc)

    def mark_dirty(self) -> None:
        """Mark history as modified and schedule a debounced flush."""
        if self._history_file is None:
            return
        self._dirty = True
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
        """Flush pending history changes immediately when dirty."""
        async with self._save_lock:
            if not self._dirty:
                return
            snapshot = {k: list(v) for k, v in self._history.items()}
            ts_snapshot = {k: list(v) for k, v in self._timestamps.items()}
            self._dirty = False
        await asyncio.to_thread(self.save_from_snapshot, snapshot, ts_snapshot)

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
