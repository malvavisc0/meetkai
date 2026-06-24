import hmac
import logging
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request

logger = logging.getLogger(__name__)

WebhookHandler = Callable[[dict], Awaitable[None]]

_HMAC_ALGORITHMS: dict[str, str] = {
    "sha256": "sha256",
    "sha512": "sha512",
}
_MAX_BODY_BYTES = 1 * 1024 * 1024


def create_webhook_app(
    hmac_key: str | None = None,
    hmac_algorithm: str = "sha512",
    webhook_path: str = "/webhook/waha",
    on_message: WebhookHandler | None = None,
) -> FastAPI:
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

        if hmac_key:
            signature = request.headers.get("X-Webhook-Hmac", "")
            algo = _HMAC_ALGORITHMS.get(hmac_algorithm.lower(), "sha512")
            expected = hmac.new(hmac_key.encode(), body, algo).hexdigest()
            if not signature or not hmac.compare_digest(signature, expected):
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

    return app
