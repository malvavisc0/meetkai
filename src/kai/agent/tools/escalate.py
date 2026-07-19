"""Escalation and blacklist tools — side-effecting tools
that alert the operator and modify bot runtime state."""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from kai.agent.context import ToolContext
from kai.agent.helpers import parse_iso, to_iso

logger = logging.getLogger(__name__)

Severity = Literal["low", "medium", "high", "critical"]


class Escalation(BaseModel):
    """A single escalation event."""

    id: str = ""
    chat_id: str
    conversation_id: str = ""
    reason: str
    severity: Severity = "medium"
    summary: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    resolved: bool = False
    resolved_at: datetime | None = None
    resolved_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = self.model_dump()
        d["created_at"] = to_iso(self.created_at)
        d["resolved_at"] = to_iso(self.resolved_at) if self.resolved_at else None
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Escalation":
        created_at = (
            parse_iso(str(data["created_at"])) if data.get("created_at") else datetime.now(UTC)
        )
        return cls(
            id=str(data.get("id", "")),
            chat_id=str(data.get("chat_id", "")),
            conversation_id=str(data.get("conversation_id", "")),
            reason=str(data.get("reason", "")),
            severity=data.get("severity", "medium"),
            summary=str(data.get("summary", "")),
            created_at=created_at,
            resolved=bool(data.get("resolved", False)),
            resolved_at=parse_iso(str(data["resolved_at"])) if data.get("resolved_at") else None,
            resolved_by=data.get("resolved_by"),
        )


class EscalationStore:
    """Persistent store of escalation events.

    Writes are atomic (temp file + replace). ``path=None`` keeps the store
    in-memory only — the API is identical either way. Mirrors
    :class:`kai.agent.scheduler.TaskStore`.
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._escalations: dict[str, Escalation] = {}
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
            for item in raw.get("escalations", []):
                if not isinstance(item, dict) or "id" not in item:
                    continue
                esc = Escalation.from_dict(item)
                self._escalations[esc.id] = esc
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("Failed to load escalations from %s: %s", self._path, exc)

    def _save_locked(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {"escalations": [e.to_dict() for e in self._escalations.values()]}
            tmp = Path(f"{self._path}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            logger.warning("Failed to save escalations to %s: %s", self._path, exc)

    async def add(self, escalation: Escalation) -> None:
        async with self._lock_for():
            self._escalations[escalation.id] = escalation
            self._save_locked()

    async def resolve(self, escalation_id: str, *, resolved_by: str | None = None) -> bool:
        async with self._lock_for():
            esc = self._escalations.get(escalation_id)
            if esc is None or esc.resolved:
                return False
            updated = esc.model_copy(
                update={
                    "resolved": True,
                    "resolved_at": datetime.now(UTC),
                    "resolved_by": resolved_by,
                }
            )
            self._escalations[escalation_id] = updated
            self._save_locked()
            return True

    async def list_all(self) -> list[Escalation]:
        async with self._lock_for():
            escs = list(self._escalations.values())
        escs.sort(key=lambda e: e.created_at)
        return escs

    async def list_active(self) -> list[Escalation]:
        return [e for e in await self.list_all() if not e.resolved]

    async def list_for_chat(self, chat_id: str) -> list[Escalation]:
        return [e for e in await self.list_all() if e.chat_id == chat_id]

    async def clear(self) -> None:
        """Remove all escalation events. Used for testing."""
        async with self._lock_for():
            self._escalations.clear()
            self._save_locked()

    def active_count(self) -> int:
        """Sync count of unresolved escalations. Used by the cockpit's sidebar."""
        return sum(1 for e in self._escalations.values() if not e.resolved)

    @staticmethod
    def new_id() -> str:
        return f"esc-{uuid.uuid4().hex[:12]}"


# Populated from Bot.configure(). Tools (created as closures in get_tools())
# read these attributes at call time.
class _State:
    def __init__(self) -> None:
        self.blacklist: list[str] | None = None
        self.on_escalation: Callable[[Escalation], Awaitable[None]] | None = None
        self.tool_context: ToolContext | None = None
        self.store: EscalationStore = EscalationStore(None)
        self.cockpit_url: str = ""
        self.cockpit_escalation_secret: str = ""


_DYN = _State()


def _resolve_chat_id(override: str | None = None) -> str:
    if override and override.strip():
        return override.strip()
    ctx = _DYN.tool_context
    if ctx is not None:
        current = ctx.current()
        if current.chat_id:
            return current.chat_id
    return ""


# ── Public API (called from Bot.configure()) ─────────────────────────


def set_escalation_handler(
    handler: Callable[[Escalation], Awaitable[None]],
) -> None:
    """Register the bot's escalation callback."""
    _DYN.on_escalation = handler


def set_blacklist(blacklist: list[str]) -> None:
    """Give tools access to the bot's mutable blacklist list."""
    _DYN.blacklist = blacklist


def set_tool_context(ctx: ToolContext | None) -> None:
    """Give tools access to the current chat context."""
    _DYN.tool_context = ctx


def set_escalation_store(store: EscalationStore) -> None:
    """Point the tools at a (possibly persistent) EscalationStore.

    Called from ``BaseBot.setup_escalation_store()``. Until this is called,
    tools use a process-lifetime in-memory store (see ``_State.store``).
    """
    _DYN.store = store


def set_cockpit_url(url: str) -> None:
    """Set the cockpit base URL for escalation forwarding.

    Called from ``BaseBot.setup_escalation_store()`` (which reads
    ``Settings.cockpit_url``). Empty string (the default for a standalone
    ``kai start``) disables forwarding.
    """
    _DYN.cockpit_url = url.rstrip("/") if url else ""


def set_escalation_secret(secret: str) -> None:
    """Set the shared secret for the escalation webhook.

    Called from ``BaseBot.setup_escalation_store()`` (which reads
    ``Settings.cockpit_escalation_secret``). Empty string disables auth.
    """
    _DYN.cockpit_escalation_secret = secret


# ── Tool functions (bound in get_tools()) ────────────────────────────


async def escalate(
    reason: str,
    severity: Severity = "medium",
    summary: str = "",
) -> str:
    """Alert the operator that this conversation needs human attention.

    Call this BEFORE choosing your action (reply/silent/etc). The escalation
    is a side-channel alert — it does not change what you say to the user.
    You can reply to the user AND escalate in the same turn.

    Use when:
    - The user explicitly asks for a human / to speak to someone
    - The conversation involves threats, legal issues, or safety concerns
    - You cannot answer and the question is important enough to warrant human review
    - Your escalation_rules (if configured) trigger

    Do NOT use for:
    - Questions you can answer with your tools
    - Casual complaints or minor issues
    - Anything you can handle yourself

    Args:
        reason: Short description of why you're escalating.
        severity: How urgent — "low", "medium", "high", or "critical".
        summary: Optional context for the operator (what was discussed).
    """
    chat_id = _resolve_chat_id()
    esc = Escalation(
        id=EscalationStore.new_id(),
        chat_id=chat_id,
        conversation_id=chat_id,
        reason=reason,
        severity=severity,
        summary=summary,
        created_at=datetime.now(UTC),
    )
    await _DYN.store.add(esc)

    handler = _DYN.on_escalation
    if handler is not None:
        try:
            await handler(esc)
        except Exception:
            logger.exception("escalation handler raised")

    return f"escalation recorded (severity={severity}): {reason}"


async def blacklist(contact_id: str = "") -> str:
    """Add the current chat's contact to the blacklist to prevent further messages.

    Use for contacts that are spamming, abusive, or otherwise undesired. After
    blacklisting, the bot will silently ignore all future messages from this
    contact for the rest of the run. Only the current conversation's contact
    can be blacklisted — an explicit ``contact_id`` that doesn't match is
    refused, so a prompt-injected message can't coerce the model into
    blacklisting arbitrary contacts.

    Args:
        contact_id: The chat ID / sender ID to blacklist. Leave empty to
            blacklist the current conversation's contact.
    """
    explicit = contact_id.strip()
    current = _resolve_chat_id()
    resolved = explicit or current
    if not resolved:
        return "Error: no contact_id provided and no current conversation context."
    if explicit and current and explicit != current:
        return (
            f"Error: cannot blacklist {explicit} — only the current "
            f"conversation's contact ({current}) can be blacklisted."
        )

    bl = _DYN.blacklist
    if bl is None:
        return "Error: blacklist not configured."
    if resolved not in bl:
        bl.append(resolved)

    esc = Escalation(
        id=EscalationStore.new_id(),
        chat_id=resolved,
        conversation_id=resolved,
        reason="contact blacklisted",
        severity="low",
        summary=f"Contact {resolved} blacklisted by the model.",
    )
    await _DYN.store.add(esc)

    handler = _DYN.on_escalation
    if handler is not None:
        try:
            await handler(esc)
        except Exception:
            logger.exception("escalation handler raised for blacklist")

    return f"contact blacklisted: {resolved}"


# ── Inspection helpers ───────────────────────────────────────────────


async def list_escalations() -> list[Escalation]:
    """Return all escalation events recorded by the current store."""
    return await _DYN.store.list_all()


async def get_active_escalations() -> list[Escalation]:
    """Return unresolved escalation events."""
    return await _DYN.store.list_active()


async def list_escalations_for_chat(chat_id: str) -> list[Escalation]:
    """Return escalation events for a specific chat."""
    return await _DYN.store.list_for_chat(chat_id)


async def resolve_escalation(esc_id: str, *, resolved_by: str | None = None) -> bool:
    """Mark an escalation as resolved. Returns True if found and unresolved."""
    return await _DYN.store.resolve(esc_id, resolved_by=resolved_by)


async def clear_escalations() -> None:
    """Remove all escalation events. Used for testing."""
    await _DYN.store.clear()


def active_escalation_count() -> int:
    """Sync count of unresolved escalations in the current store.

    Registered as a Jinja global by the cockpit so the sidebar badge renders
    on every page without an async call (see ``EscalationStore.active_count``).
    """
    return _DYN.store.active_count()


async def forward_to_cockpit(escalation: Escalation) -> None:
    """POST an escalation to the cockpit's ``/api/escalations`` webhook.

    Called from ``BaseBot.on_escalation`` so every bot forwards the same way.
    Best-effort: the escalation is already persisted locally before this runs,
    so a cockpit that's down (or ``cockpit_url`` unset) just means the cockpit
    dashboard won't show it — the bot's own store still has it. Never raises.
    """
    url = _DYN.cockpit_url
    if not url:
        return
    endpoint = f"{url}/api/escalations"
    headers: dict[str, str] = {}
    secret = _DYN.cockpit_escalation_secret
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(endpoint, json=escalation.to_dict(), headers=headers)
            if resp.status_code == 401:
                logger.warning("cockpit escalation POST rejected (bad secret): %s", endpoint)
            elif resp.status_code >= 400:
                logger.warning(
                    "cockpit escalation POST to %s returned %s: %s",
                    endpoint,
                    resp.status_code,
                    resp.text[:200],
                )
    except Exception:
        logger.debug("cockpit escalation POST to %s failed", endpoint, exc_info=True)
