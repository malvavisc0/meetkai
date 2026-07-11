"""Webhook-type catalog for cockpit-level provider ingress.

A small, hardcoded dict of ``WebhookType`` entries — not a generic plugin
system. Each entry has a hand-written ``verify_signature`` (per-provider
scheme + replay guard) and ``parse`` (per-provider payload shape). The
catalog starts empty; the first real entry (e.g. ``email`` via Resend) ships
with the first bot type that consumes it.

Replay protection for the centralized ingress is provided by
``_check_freshness``: a freshness window (±5 min from the server clock) plus
nonce deduplication. Every ``verify_signature`` implementation MUST call it
after its own signature comparison. A type whose provider supplies neither a
timestamp nor a nonce cannot be added to ``WEBHOOK_TYPES`` — the helper
rejects a call with both ``None``, surfacing the problem immediately.

The nonce cache is per-process state, correct for the single uvicorn process
the cockpit runs today. Multi-worker scaling would move it to Redis/DB.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request
from pydantic import BaseModel

FRESHNESS_WINDOW_SECONDS = 300
_SEEN_NONCES_MAX = 10000
_seen_nonces: OrderedDict[str, float] = OrderedDict()


class NormalizedMessage(BaseModel):
    """Provider-agnostic inbound event carried to the bot's ``ingest_event``."""

    source: str
    text: str
    metadata: dict = {}
    event: str = ""


@dataclass(frozen=True)
class WebhookType:
    name: str
    # Implementations MUST:
    #   1. Verify the provider's signature against the body (bespoke scheme).
    #   2. Extract the provider-native timestamp (epoch seconds) and/or nonce
    #      from the request headers/body.
    #   3. Call _check_freshness(timestamp=..., nonce=...) and return False if
    #      it rejects.
    # A type whose provider supplies neither a timestamp nor a nonce cannot
    # be added to WEBHOOK_TYPES.
    verify_signature: Callable[[Request, bytes], bool]
    parse: Callable[[dict], NormalizedMessage]


WEBHOOK_TYPES: dict[str, WebhookType] = {}


def _prune_seen(now: float) -> None:
    """Evict nonce entries older than the freshness window.

    Relies on insertion order matching timestamp order: in production
    ``_check_freshness`` stores ``time.time()`` (monotonic-ish), so the
    oldest-inserted entry is also the oldest by value. Pop from the front
    until the front entry is still fresh.
    """
    cutoff = now - FRESHNESS_WINDOW_SECONDS
    while _seen_nonces and next(iter(_seen_nonces.values())) < cutoff:
        _seen_nonces.popitem(last=False)


def _check_freshness(
    *, timestamp: float | None = None, nonce: str | None = None, now: float | None = None
) -> bool:
    """Reject stale and replayed webhook requests.

    Returns True if the request is fresh and not a replay, False otherwise.
    At least one of ``timestamp`` or ``nonce`` must be provided — a call with
    both ``None`` returns False (fail closed), because a type whose provider
    supplies neither is unprotectable and must not be in ``WEBHOOK_TYPES``.

    ``now`` is a test seam; production callers omit it (the helper uses
    ``time.time()``).
    """
    if timestamp is None and nonce is None:
        return False
    t = now if now is not None else time.time()
    _prune_seen(t)
    if timestamp is not None and abs(t - timestamp) > FRESHNESS_WINDOW_SECONDS:
        return False
    if nonce is not None:
        if nonce in _seen_nonces:
            return False
        _seen_nonces[nonce] = t
        if len(_seen_nonces) > _SEEN_NONCES_MAX:
            _seen_nonces.popitem(last=False)
    return True


def _clear_seen_nonces() -> None:
    """Drop all cached nonces (test use only)."""
    _seen_nonces.clear()
