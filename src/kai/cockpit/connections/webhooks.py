"""Webhook-type catalog for cockpit-level provider ingress."""

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


def _strip_whsec_prefix(secret: str) -> str:
    """Strip the ``whsec_`` prefix Resend/Svix prepends to signing secrets."""
    return secret[len("whsec_") :] if secret.startswith("whsec_") else secret


class NormalizedMessage(BaseModel):
    """Provider-agnostic inbound event passed from the cockpit to the bot."""

    source: str
    text: str
    metadata: dict = {}
    event: str = ""


@dataclass(frozen=True)
class WebhookType:
    name: str
    # Verify the provider's signature; call _check_freshness after.
    verify_signature: Callable[[Request, bytes, str], bool]
    # Parse raw webhook JSON + decrypted connection config dict into NormalizedMessage.
    parse: Callable[[dict, dict], NormalizedMessage]
    # Request header carrying the provider's idempotency nonce (e.g. "svix-id").
    # Empty = no nonce dedup (timestamp-only).
    nonce_header: str = ""


def _prune_seen(now: float) -> None:
    """Evict nonce entries older than the freshness window."""
    cutoff = now - FRESHNESS_WINDOW_SECONDS
    while _seen_nonces and next(iter(_seen_nonces.values())) < cutoff:
        _seen_nonces.popitem(last=False)


def _check_freshness(
    *, timestamp: float | None = None, nonce: str | None = None, now: float | None = None
) -> bool:
    """Reject stale (past freshness window) and replayed (duplicate nonce) requests.

    Returns True if fresh and not a replay. Fails closed when both
    ``timestamp`` and ``nonce`` are None — a type without either is unprotectable.
    ``now`` is a test seam; production callers omit it.
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
    """Return True if ``nonce`` was recorded by a prior successful forward."""
    _prune_seen(time.time())
    return nonce in _seen_nonces


def record_nonce(nonce: str, now: float | None = None) -> None:
    """Record a nonce after a successful forward for retry dedup.

    Only called by the route after ``forward_event`` returns True — a transient
    bot failure leaves the nonce unrecorded so retries get a clean re-forward.
    """
    t = now if now is not None else time.time()
    _prune_seen(t)
    _seen_nonces[nonce] = t
    if len(_seen_nonces) > _SEEN_NONCES_MAX:
        _seen_nonces.popitem(last=False)


def _sign_resend(svix_id: str, svix_timestamp: str, body: bytes, secret: str) -> str:
    """Produce the ``v1,<base64-mac>`` signature for a Resend/Svix payload.

    Shared between ``_verify_resend`` and ``EmailConnectionsService.test()``
    so producer and verifier can never drift. Raises ``ValueError`` on
    invalid base64 secret.
    """
    signed = f"{svix_id}.{svix_timestamp}.".encode() + body
    key = base64.b64decode(_strip_whsec_prefix(secret))  # raises on invalid base64
    mac = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return f"v1,{mac}"


def _verify_resend(request: Request, body: bytes, secret: str) -> bool:
    """Verify a Resend (Svix-signed) inbound webhook.

    Decodes the base64 signing secret, computes HMAC-SHA256 over
    ``{svix-id}.{svix-timestamp}.{raw_body}``, and compares against the
    ``svix-signature`` header candidates. Timestamp freshness is checked via
    ``_check_freshness``; nonce dedup is owned by the route.
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
    """Provider's ``parse`` needed an upstream API call and it failed.

    Distinct from a malformed payload — this is an upstream dependency failure.
    The route surfaces it as 502.
    """


_RESEND_API_BASE = "https://api.resend.com"


def _fetch_resend_email(email_id: str, api_key: str) -> dict:
    """GET /emails/receiving/{id} — the only source of body text/HTML."""
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
    """GET /emails/receiving/{id}/attachments — the only source of download URLs."""
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
    """Map the Attachments API response to the URL-only contract."""
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
    """Map a Resend ``email.received`` event to NormalizedMessage.

    The webhook carries only envelope metadata; body text/HTML and attachment
    URLs require separate REST API calls via ``cfg["api_key"]``. Non-inbound
    event types (delivery/bounce) pass through without API calls so the bot
    can reject them cheaply.
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
