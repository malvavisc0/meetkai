import hmac
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from fastapi import FastAPI, HTTPException, Request

from kai.bots.base import TellResult

logger = logging.getLogger(__name__)

WebhookHandler = Callable[[dict], Awaitable[None]]
IngestHandler = Callable[[dict], Awaitable[dict]]


class TellHandler(Protocol):
    """Matches ``Bot.handle_operator``: ``persist`` is keyword-only."""

    def __call__(self, message: str, *, persist: bool = False) -> Awaitable[TellResult]: ...


StatusHandler = Callable[[], Awaitable[dict]]
ClearHandler = Callable[[], Awaitable[dict]]
SleepHandler = Callable[[str], Awaitable[dict]]

_HMAC_ALGORITHMS: dict[str, str] = {
    "sha256": "sha256",
    "sha512": "sha512",
}
_MAX_BODY_BYTES = 1 * 1024 * 1024


def _verify_signature(hmac_key: str, hmac_algorithm: str, body: bytes, signature: str) -> bool:
    algo = _HMAC_ALGORITHMS.get(hmac_algorithm.lower(), "sha512")
    expected = hmac.new(hmac_key.encode(), body, algo).hexdigest()
    return bool(signature) and hmac.compare_digest(signature, expected)


def create_webhook_app(
    hmac_key: str,
    hmac_algorithm: str = "sha512",
    webhook_path: str = "/webhook/waha",
    on_message: WebhookHandler | None = None,
    on_tell: TellHandler | None = None,
    on_ingest: IngestHandler | None = None,
    on_status: StatusHandler | None = None,
    on_clear: ClearHandler | None = None,
    on_sleep: SleepHandler | None = None,
    on_wake: SleepHandler | None = None,
) -> FastAPI:
    """Create the shared webhook FastAPI app.

    ``hmac_key`` is **mandatory**: the inbound WAHA webhook and the operator
    ``/tell``, ``/status`` and ``/clear`` routes share this single secret and
    are all HMAC-verified. ``on_tell`` wires a bot's ``handle_operator``;
    when ``None`` the ``/tell`` route answers 404 (the bot opts out of
    operator control). ``on_ingest`` wires a bot's ``ingest_event`` (forwarded
    by the cockpit's centralized ``/webhook/{slug}/{type}`` ingress); when
    ``None`` the ``/ingest`` route answers 404 (the bot opts out of centralized
    webhook ingest — e.g. WAHA). ``on_status`` wires a bot's
    ``status_snapshot``; when ``None`` the ``/status`` route answers 404.
    ``on_clear`` wires a bot's history-reset hook; when ``None`` the ``/clear``
    route answers 404. ``on_sleep``/``on_wake`` toggle a chat's sleep state;
    when ``None`` the matching route answers 404.
    """
    app = FastAPI(title="kai webhook")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post(webhook_path)
    async def webhook(request: Request):
        body = await request.body()

        if len(body) > _MAX_BODY_BYTES:
            logger.warning("Oversized webhook body rejected: %d bytes", len(body))
            raise HTTPException(status_code=413, detail="payload too large")

        signature = request.headers.get("X-Webhook-Hmac", "")
        if not _verify_signature(hmac_key, hmac_algorithm, body, signature):
            logger.warning("Invalid webhook signature")
            raise HTTPException(status_code=401, detail="Invalid signature")

        try:
            payload = await request.json()
        except Exception:
            logger.warning("Malformed webhook body")
            return {"status": "error", "detail": "malformed body"}

        event = payload.get("event")

        if event == "message":
            logger.info(
                "Received message from %s",
                payload.get("payload", {}).get("from", "unknown"),
            )
            if on_message:
                try:
                    await on_message(payload)
                except Exception:
                    logger.exception("Handler error for message event (returning 200)")
        else:
            logger.debug("Ignoring event: %s", event)

        return {"status": "ok"}

    @app.post("/tell")
    async def tell(request: Request):
        """Operator control route.

        HMAC-verified with the same key as the inbound webhook. The body is
        ``{"message": str, "persist": bool}``; the route relays it to the
        bot's ``handle_operator`` and returns the bot's structured
        :class:`TellResult` verbatim. The agent decides the delivery target
        through its structured action output (e.g. ``action.target``), not
        through a request field. The framework does not interpret the result.
        """
        if on_tell is None:
            raise HTTPException(status_code=404, detail="tell not supported by this bot")

        body = await request.body()
        if len(body) > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="payload too large")

        signature = request.headers.get("X-Webhook-Hmac", "")
        if not _verify_signature(hmac_key, hmac_algorithm, body, signature):
            logger.warning("Invalid /tell signature")
            raise HTTPException(status_code=401, detail="Invalid signature")

        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="malformed body")

        message = str(payload.get("message", "")).strip()
        persist_raw = payload.get("persist", False)
        if not isinstance(persist_raw, bool):
            raise HTTPException(status_code=400, detail="persist must be a boolean")
        persist = persist_raw
        if not message:
            raise HTTPException(status_code=400, detail="message is required")

        try:
            result = await on_tell(message, persist=persist)
        except Exception:
            logger.exception("on_tell handler error")
            return TellResult(ok=False, reply="operator turn failed").model_dump()

        if isinstance(result, TellResult):
            return result.model_dump()
        return result

    @app.post("/ingest")
    async def ingest(request: Request):
        """Forwarded-event route for the cockpit's centralized webhook ingress.

        HMAC-verified with the same key as the inbound webhook. The body is a
        ``NormalizedMessage`` dict forwarded by the cockpit's
        ``/webhook/{slug}/{type}`` route. When ``on_ingest`` is ``None`` the
        bot opts out (404) — e.g. WAHA, which keeps its own bespoke webhook.
        """
        if on_ingest is None:
            raise HTTPException(status_code=404, detail="ingest not supported by this bot")

        body = await request.body()
        if len(body) > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="payload too large")

        signature = request.headers.get("X-Webhook-Hmac", "")
        if not _verify_signature(hmac_key, hmac_algorithm, body, signature):
            raise HTTPException(status_code=401, detail="Invalid signature")

        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="malformed body")

        try:
            return await on_ingest(payload)
        except NotImplementedError:
            raise HTTPException(status_code=404, detail="ingest not supported by this bot")
        except Exception:
            logger.exception("on_ingest handler error")
            raise HTTPException(status_code=500, detail="ingest failed")

    @app.get("/status")
    async def status(request: Request):
        """Operator status route.

        HMAC-verified with the same key as the inbound webhook. There is no
        request body, so the signature is computed over an empty byte string;
        the CLI sends ``X-Webhook-Hmac`` accordingly. Returns the bot's
        structured status snapshot verbatim (see ``status_snapshot``).
        """
        if on_status is None:
            raise HTTPException(status_code=404, detail="status not supported by this bot")

        signature = request.headers.get("X-Webhook-Hmac", "")
        if not _verify_signature(hmac_key, hmac_algorithm, b"", signature):
            logger.warning("Invalid /status signature")
            raise HTTPException(status_code=401, detail="Invalid signature")

        try:
            return await on_status()
        except Exception:
            logger.exception("on_status handler error")
            raise HTTPException(status_code=500, detail="status failed")

    @app.post("/clear")
    async def clear(request: Request):
        """Operator history-reset route.

        HMAC-verified with the same key as the inbound webhook, over an empty
        body (like ``/status``). Resets the bot's ``operator`` conversation
        bucket so a chat session can start fresh without restarting the bot.
        """
        if on_clear is None:
            raise HTTPException(status_code=404, detail="clear not supported by this bot")

        signature = request.headers.get("X-Webhook-Hmac", "")
        if not _verify_signature(hmac_key, hmac_algorithm, b"", signature):
            logger.warning("Invalid /clear signature")
            raise HTTPException(status_code=401, detail="Invalid signature")

        try:
            return await on_clear()
        except Exception:
            logger.exception("on_clear handler error")
            raise HTTPException(status_code=500, detail="clear failed")

    def _make_sleep_toggle_route(action: str, handler: SleepHandler | None):
        @app.post(f"/{action}")
        async def route(request: Request):
            if handler is None:
                raise HTTPException(status_code=404, detail=f"{action} not supported by this bot")

            body = await request.body()
            if len(body) > _MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="payload too large")
            signature = request.headers.get("X-Webhook-Hmac", "")
            if not _verify_signature(hmac_key, hmac_algorithm, body, signature):
                logger.warning("Invalid /%s signature", action)
                raise HTTPException(status_code=401, detail="Invalid signature")
            try:
                payload = await request.json()
            except Exception:
                raise HTTPException(status_code=400, detail="malformed body")
            chat_id = str(payload.get("chat_id", "")).strip()
            if not chat_id:
                raise HTTPException(status_code=400, detail="chat_id is required")
            try:
                return await handler(chat_id)
            except Exception:
                logger.exception("on_%s handler error", action)
                raise HTTPException(status_code=500, detail=f"{action} failed")

    _make_sleep_toggle_route("sleep", on_sleep)
    _make_sleep_toggle_route("wake", on_wake)

    return app
