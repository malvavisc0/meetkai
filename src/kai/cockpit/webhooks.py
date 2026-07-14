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

import base64
import hashlib
import hmac
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup
from fastapi import Request
from pydantic import BaseModel

FRESHNESS_WINDOW_SECONDS = 300
_SEEN_NONCES_MAX = 10000
_seen_nonces: OrderedDict[str, float] = OrderedDict()


class NormalizedMessage(BaseModel):
    """Provider-agnostic inbound event carried to the bot's ingest_event.

    This is the contract between the cockpit (normalize) and the bot
    (consume). Every ``WebhookType.parse()`` produces one; every bot's
    ``ingest_event()`` consumes one.

    Fields:
        source: The sender's identifier in the provider's system
                (email address, phone number, username). Used as the
                conversation_id for history tracking.
        text:   The message body, plaintext. HTML stripped if no text part.
        metadata: Provider-specific fields the bot may need:
                  - message_id, subject, to (email-specific)
                  - attachments: list of {"url", "content_type", "filename"}
                    (URLs only — the bot downloads bytes)
        event:  The event type (e.g. "email.inbound"). Bots use this to
                decide whether to act or return {"ok": False} for
                unsupported events.
    """

    source: str
    text: str
    metadata: dict = {}
    event: str = ""


@dataclass(frozen=True)
class WebhookType:
    name: str
    # Implementations MUST:
    #   1. Verify the provider's signature against the body (bespoke scheme),
    #      using the decrypted per-operator ``secret`` (third arg).
    #   2. Extract the provider-native timestamp (epoch seconds) and/or nonce
    #      from the request headers/body.
    #   3. Call _check_freshness(timestamp=..., nonce=None) and return False if
    #      it rejects. verify_signature records NO nonce — the route owns the
    #      nonce dedup set via is_nonce_seen/record_nonce (see nonce_header).
    # A type whose provider supplies neither a timestamp nor a nonce cannot
    # be added to WEBHOOK_TYPES.
    verify_signature: Callable[[Request, bytes, str], bool]
    # ``parse`` takes the raw webhook JSON and the *decrypted connection
    # config dict* (whatever secret fields that WebhookConnectionType
    # declares — e.g. Resend needs both a signing secret, already used by
    # verify_signature, and a separate API key to fetch email content).
    # Passing the whole config dict (not a single named field) keeps this
    # generic: the next provider reads whatever keys it declared without a
    # signature change here.
    parse: Callable[[dict, dict], NormalizedMessage]
    # The request header that carries the provider's idempotency nonce
    # (e.g. Svix's "svix-id"). The centralized ingress route reads this header
    # after verification and dedups on it — keeping the per-provider header name
    # on the WebhookType means the route stays generic for the next provider
    # (no if-branch per type). Empty = no nonce dedup (timestamp-only).
    nonce_header: str = ""


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


def is_nonce_seen(nonce: str) -> bool:
    """True if ``nonce`` was recorded by a prior successful forward.

    Used by the centralized ingress route after signature verification:
    a seen nonce means the provider is retrying an event already delivered,
    so the route answers 202 ``{"deduped": true}`` instead of re-forwarding.
    """
    _prune_seen(time.time())
    return nonce in _seen_nonces


def record_nonce(nonce: str, now: float | None = None) -> None:
    """Record a nonce after a successful forward, so provider retries dedup.

    Records ``now`` (defaulting to ``time.time()``), prunes stale entries, and
    bounds the cache to ``_SEEN_NONCES_MAX``. Called by the route ONLY after
    ``forward_event`` returns True — a transient bot failure (502) leaves the
    nonce unrecorded so the provider's retry of the same id gets a clean
    re-forward attempt rather than a permanent 202 dedup.
    """
    t = now if now is not None else time.time()
    _prune_seen(t)
    _seen_nonces[nonce] = t
    if len(_seen_nonces) > _SEEN_NONCES_MAX:
        _seen_nonces.popitem(last=False)


def _sign_resend(svix_id: str, svix_timestamp: str, body: bytes, secret: str) -> str:
    """Produce the ``v1,<base64-mac>`` signature for a Resend/Svix payload.

    Shared between ``_verify_resend`` (verifier) and
    ``EmailConnectionsService.test()`` (self-loopback signer) so the
    producer and verifier can never drift. Returns the full candidate
    string (``v1,<mac>``). Raises ``ValueError`` if the secret isn't valid
    base64.
    """
    signed = f"{svix_id}.{svix_timestamp}.".encode() + body
    key = base64.b64decode(secret)  # raises on invalid base64
    mac = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return f"v1,{mac}"


def _verify_resend(request: Request, body: bytes, secret: str) -> bool:
    """Verify a Resend (Svix-signed) inbound webhook.

    Svix scheme: header ``svix-signature`` is a space-separated list of
    ``v1,<base64-mac>`` candidates; the signing secret (from the
    Resend/Svix dashboard) is **base64-encoded** and must be decoded before
    use as the HMAC key; the signed message is
    ``{svix-id}.{svix-timestamp}.{raw_body}``; the MAC is HMAC-SHA256 →
    base64-encode → compare (stripping the ``v1,`` prefix) with
    ``hmac.compare_digest``. The body bytes are the **raw** request body.

    Checks the timestamp window via ``_check_freshness(timestamp=...,
    nonce=None)`` — no nonce is recorded here; the route owns nonce dedup.
    """
    svix_id = request.headers.get("svix-id", "")
    svix_ts = request.headers.get("svix-timestamp", "")
    svix_sig = request.headers.get("svix-signature", "")
    if not svix_id or not svix_ts or not svix_sig:
        return False
    try:
        ts = int(svix_ts)
    except ValueError:
        return False
    if not _check_freshness(timestamp=ts, nonce=None):
        return False
    try:
        expected = _sign_resend(svix_id, svix_ts, body, secret)
    except Exception:
        return False
    expected_mac = expected[3:]  # strip "v1," for comparison
    for cand in svix_sig.split():
        if cand.startswith("v1,"):
            cand = cand[3:]
        if hmac.compare_digest(cand, expected_mac):
            return True
    return False


class WebhookUpstreamError(Exception):
    """A provider's ``parse`` needed an upstream API call and it failed.

    Distinct from a malformed webhook payload — this is an upstream
    dependency failure (e.g. Resend's Received-Emails/Attachments API being
    unreachable or erroring), not an attacker-controlled input problem. The
    route surfaces it as 502, not 400. Generic across providers so the route
    doesn't need a per-provider except clause.
    """


_RESEND_API_BASE = "https://api.resend.com"


def _fetch_resend_email(email_id: str, api_key: str) -> dict:
    """GET /emails/receiving/{id} — the only source of body text/HTML.

    Resend's inbound webhook carries envelope metadata only (no body, no
    headers, no attachment content) — the docs are explicit about this. The
    body must be fetched separately with the operator's Resend API key.
    """
    try:
        resp = httpx.get(
            f"{_RESEND_API_BASE}/emails/receiving/{email_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        raise WebhookUpstreamError(
            f"could not reach Resend API for email {email_id}: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise WebhookUpstreamError(
            f"Resend API returned {resp.status_code} fetching email {email_id}"
        )
    return resp.json()


def _fetch_resend_attachments(email_id: str, api_key: str) -> list[dict]:
    """GET /emails/receiving/{id}/attachments — the only source of download URLs.

    The webhook's ``data.attachments`` and the email-fetch's ``attachments``
    both omit a download URL (id/filename/content_type/content_disposition/
    content_id only) — only this dedicated endpoint returns ``download_url``.
    """
    try:
        resp = httpx.get(
            f"{_RESEND_API_BASE}/emails/receiving/{email_id}/attachments",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        raise WebhookUpstreamError(
            f"could not reach Resend API for attachments of {email_id}: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise WebhookUpstreamError(
            f"Resend API returned {resp.status_code} fetching attachments for {email_id}"
        )
    return resp.json().get("data", [])


def _extract_attachments(attachments: list[dict]) -> list[dict]:
    """Map the Attachments API's list response to the URL-only contract.

    ``attachments`` here is the ``data`` list from
    ``_fetch_resend_attachments`` (each item has ``download_url``) — not the
    webhook's or the email-fetch's attachment stubs, neither of which carry
    a URL. Returns ``[{"url", "content_type", "filename"}, ...]``.
    """
    return [
        {
            "url": att.get("download_url", ""),
            "content_type": att.get("content_type", ""),
            "filename": att.get("filename", ""),
        }
        for att in attachments
    ]


def _extract_email_body(email: dict) -> str:
    """Prefer the text part; fall back to stripped HTML if no text part."""
    text = email.get("text", "")
    if text:
        return text
    html = email.get("html", "")
    if html:
        # Strip HTML to plaintext — BeautifulSoup is already a dependency.
        return BeautifulSoup(html, "html.parser").get_text(separator=" ").strip()
    return ""


def _parse_resend(payload: dict, cfg: dict) -> NormalizedMessage:
    """Map a Resend webhook event to NormalizedMessage.

    The webhook body shape is ``{"type": ..., "created_at": ..., "data":
    {...}}`` — inbound email fields (``email_id``, ``from``, ``to``,
    ``subject``, ``message_id``, ``attachments`` stubs) live under ``data``.
    Per Resend's docs, the webhook never carries the body text/HTML or an
    attachment download URL; both require follow-up calls to the Received
    Emails / Attachments REST APIs using the operator's Resend API key
    (``cfg["api_key"]``, decrypted by the route from the connection row).

    Only ``type == "email.received"`` is an inbound message the bot acts
    on — a Resend webhook endpoint can also receive delivery/bounce events
    if the operator subscribes to more than inbound. Those pass through
    with their own ``event`` (not ``"email.inbound"``) and no API calls, so
    the bot's ``ingest_event`` can reject them without spending an API
    round-trip on content it will discard.
    """
    event_type = payload.get("type", "")
    data = payload.get("data", {})
    if event_type != "email.received":
        return NormalizedMessage(source=data.get("from", ""), text="", event=event_type)

    api_key = cfg.get("api_key", "")
    email_id = data["email_id"]
    email = _fetch_resend_email(email_id, api_key)
    attachments = _fetch_resend_attachments(email_id, api_key) if data.get("attachments") else []

    return NormalizedMessage(
        source=data["from"],
        text=_extract_email_body(email),
        metadata={
            "message_id": data.get("message_id", ""),
            "subject": data.get("subject", ""),
            "to": data.get("to", []),
            "attachments": _extract_attachments(attachments),
        },
        event="email.inbound",
    )


WEBHOOK_TYPES: dict[str, WebhookType] = {
    "resend": WebhookType(
        name="resend",
        verify_signature=_verify_resend,
        parse=_parse_resend,
        nonce_header="svix-id",
    ),
}
