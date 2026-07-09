import base64
import logging
from urllib.parse import quote

import httpx

from kai.bots.waha.config import WahaSettings, get_waha_settings

logger = logging.getLogger(__name__)


class WahaClient:
    def __init__(self, settings: WahaSettings | None = None) -> None:
        self.settings = settings or get_waha_settings()
        self.base_url = self.settings.url.rstrip("/")
        self.session = self.settings.session
        headers: dict[str, str] = {}
        if self.settings.api_key:
            headers["X-Api-Key"] = self.settings.api_key
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=30.0, headers=headers)

    async def close(self) -> None:
        await self._client.aclose()

    async def get_sessions(self) -> list[dict]:
        resp = await self._client.get("/api/sessions")
        resp.raise_for_status()
        return resp.json()

    async def get_session_status(self) -> dict | None:
        sessions = await self.get_sessions()
        for s in sessions:
            if s.get("name") == self.session:
                return s
        return None

    # --- Provisioning methods (cockpit connections service) ---
    # These take an explicit name and never touch self.session.

    async def create_session(self, name: str, webhook_config: dict | None = None) -> dict:
        """POST /api/sessions — create + start + set webhook in one call.

        ``start: true`` is explicit because WAHA defaults to NOT starting the
        session on creation in some versions, leaving it STOPPED — which makes
        the QR endpoint 422 ("expected SCAN_QR_CODE"). Starting here drives
        the session through STARTING -> SCAN_QR_CODE so the QR is fetchable.

        If the session already exists (422 — e.g. it survived a WAHA
        container restart while the cockpit Connection row was reset, or the
        user clicked connect twice), fall back to PUT (update webhook) + start
        so reconnect is idempotent instead of erroring out.
        """
        payload: dict = {"name": name, "start": True, "config": {}}
        if webhook_config:
            payload["config"]["webhooks"] = [webhook_config]
        resp = await self._client.post("/api/sessions", json=payload)
        if resp.status_code == 422:
            # Session already exists — update its webhook config and (re)start.
            update_payload: dict = {"config": {}}
            if webhook_config:
                update_payload["config"]["webhooks"] = [webhook_config]
            resp = await self._client.put(f"/api/sessions/{name}", json=update_payload)
            resp.raise_for_status()
            await self.start_session(name)
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    async def get_session(self, name: str) -> dict | None:
        """GET /api/sessions/{name} — returns session info or None."""
        try:
            resp = await self._client.get(f"/api/sessions/{name}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError:
            return None

    async def start_session(self, name: str) -> None:
        """POST /api/sessions/{name}/start — (re)start a STOPPED session.

        A STOPPED session will never reach SCAN_QR_CODE on its own, so the
        QR endpoint 422s. This drives it back through STARTING ->
        SCAN_QR_CODE. Safe to call on an already-starting session.
        """
        resp = await self._client.post(f"/api/sessions/{name}/start")
        # 200/201 = started; 409 = already started/starting — both fine.
        if resp.status_code not in (200, 201, 409):
            resp.raise_for_status()

    async def get_qr(self, name: str) -> bytes:
        """GET /api/{name}/auth/qr?format=image — returns PNG bytes."""
        resp = await self._client.get(f"/api/{name}/auth/qr", params={"format": "image"})
        resp.raise_for_status()
        return resp.content

    async def delete_session(self, name: str) -> None:
        """DELETE /api/sessions/{name} — tear down a WAHA session."""
        resp = await self._client.delete(f"/api/sessions/{name}")
        resp.raise_for_status()

    # --- End provisioning methods ---

    def _build_webhook_config(self, webhook_url: str) -> dict:
        webhook_cfg: dict = {
            "url": webhook_url,
            "events": ["message"],
        }
        if self.settings.hmac_key:
            webhook_cfg["hmac"] = {
                "key": self.settings.hmac_key,
                "algorithm": self.settings.hmac_algorithm,
            }
        return {"webhooks": [webhook_cfg]}

    async def update_session_webhook(self, webhook_url: str) -> dict:
        logger.info("Updating WAHA session webhook: %s", self.session)
        resp = await self._client.put(
            f"/api/sessions/{self.session}",
            json={"config": self._build_webhook_config(webhook_url)},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_profile(self) -> dict | None:
        resp = await self._client.get(f"/api/{self.session}/profile")
        if resp.status_code == 200:
            return resp.json()
        return None

    async def get_profile_picture(self) -> bytes | None:
        try:
            resp = await self._client.get(f"/api/{self.session}/profile/picture")
            if resp.status_code == 200 and resp.content and len(resp.content) > 100:
                return resp.content
        except Exception as exc:
            logger.debug("Profile picture endpoint failed: %s", exc)
        return None

    async def download_image(self, url: str) -> bytes | None:
        try:
            resp = await self._client.get(url, follow_redirects=True)
            if resp.status_code == 200:
                return resp.content
        except Exception as exc:
            logger.debug("Failed to download image from %s: %s", url, exc)
        return None

    async def download_media(self, media_url: str, max_size_mb: int = 10) -> bytes | None:
        max_bytes = max_size_mb * 1024 * 1024
        try:
            async with self._client.stream("GET", media_url, follow_redirects=True) as resp:
                if resp.status_code != 200:
                    logger.warning(
                        "Media download failed (status=%d): %s", resp.status_code, media_url
                    )
                    return None
                content_length = resp.headers.get("content-length")
                if content_length and int(content_length) > max_bytes:
                    logger.warning(
                        "Media too large (%s bytes, max %d MB): %s",
                        content_length,
                        max_size_mb,
                        media_url,
                    )
                    return None
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes(8192):
                    total += len(chunk)
                    if total > max_bytes:
                        logger.warning("Media download exceeded %d MB, aborting", max_size_mb)
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except Exception as exc:
            logger.warning("Failed to download media from %s: %s", media_url, exc)
            return None

    async def get_chat_participants(self, chat_id: str) -> list[dict]:
        """Fetch the participant list of a group chat.

        Hits the WAHA v2 participants endpoint. The chat id is URL-encoded
        because it contains an ``@`` (e.g. ``...@g.us``). Returns the raw
        participant objects; callers extract the JID and display name.
        """
        encoded = quote(chat_id, safe="")
        resp = await self._client.get(f"/api/{self.session}/groups/{encoded}/participants/v2")
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    async def get_chat_messages(
        self,
        chat_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        download_media: bool = False,
    ) -> list[dict]:
        """Fetch messages from a chat's history.

        Wraps ``GET /api/{session}/chats/{chatId}/messages``. Returns messages
        newest-first (``sortOrder=desc``) so an ``offset`` of 0 yields the most
        recent batch; callers that want chronological order reverse the list.
        Media downloads are off by default — a recap/summary only needs text
        bodies, and downloads are slow and large. ``merge=true`` collapses
        ``@lid``/``@c.us`` duplicates so sender identity is consistent with the
        live roster.

        Args:
            chat_id: The chat JID (e.g. ``123@g.us``).
            limit: Max messages to return (WAHA requires this).
            offset: Skip this many recent messages to page into older history.
            download_media: If True, WAHA downloads media for each message.

        Returns:
            Raw WAHA message dicts (newest first), or ``[]`` on a bad response.
        """
        encoded = quote(chat_id, safe="")
        resp = await self._client.get(
            f"/api/{self.session}/chats/{encoded}/messages",
            params={
                "limit": limit,
                "offset": offset,
                "sortOrder": "desc",
                "downloadMedia": "true" if download_media else "false",
                "merge": "true",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    async def get_chats_overview(self, *, limit: int = 20, offset: int = 0) -> list[dict]:
        """Wraps ``GET /api/{session}/chats/overview``.

        Returns chat summaries (``ChatSummary``: ``id``, ``name``, ``picture``,
        ``lastMessage``) sorted by last message timestamp. ``merge=true`` is
        always passed so ``@lid`` and ``@c.us`` chats for the same contact
        collapse into one row — the picker must never show a contact twice.
        """
        resp = await self._client.get(
            f"/api/{self.session}/chats/overview",
            params={"limit": limit, "offset": offset, "merge": "true"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    async def get_message(
        self, chat_id: str, message_id: str, *, download_media: bool = True
    ) -> dict | None:
        """Fetch a single message, optionally with downloaded media.

        WAHA webhooks deliver message payloads without downloaded media (the
        ``media`` field is null and ``mediaUrl`` is absent). To resolve an
        image/voice attachment the bot must re-fetch the message from the REST
        API with ``downloadMedia=true``, which populates ``media.url`` pointing
        at a fetchable ``/api/files/...`` endpoint.

        Args:
            chat_id: The chat JID (e.g. ``123@g.us`` or ``123@c.us``).
            message_id: The message's ``id`` field.
            download_media: If True (default), WAHA downloads the media and
                returns a ``media.url``.

        Returns:
            The message dict, or None if the message cannot be found.
        """
        encoded_chat = quote(chat_id, safe="")
        encoded_msg = quote(message_id, safe="")
        params = {"downloadMedia": "true" if download_media else "false"}
        resp = await self._client.get(
            f"/api/{self.session}/chats/{encoded_chat}/messages/{encoded_msg}",
            params=params,
        )
        if resp.status_code == 404:
            logger.warning("Message not found: %s in %s", message_id, chat_id)
            return None
        resp.raise_for_status()
        return resp.json()

    async def send_message(
        self, chat_id: str, text: str, mentions: list[str] | None = None
    ) -> dict:
        logger.info("Sending message to %s: %s", chat_id, text[:100])
        body: dict = {"session": self.session, "chatId": chat_id, "text": text}
        if mentions:
            body["mentions"] = mentions
        resp = await self._client.post("/api/sendText", json=body)
        resp.raise_for_status()
        return resp.json()

    async def send_voice(
        self, chat_id: str, audio_bytes: bytes, reply_to: str | None = None
    ) -> dict:
        """Send a voice note via ``/api/sendVoice``.

        WAHA converts the audio to WhatsApp's opus format server-side when
        ``convert`` is true, so raw WAV (PCM 16-bit) is accepted directly.
        """
        logger.info("Sending voice message to %s (%d bytes)", chat_id, len(audio_bytes))
        body: dict = {
            "session": self.session,
            "chatId": chat_id,
            "file": {
                "mimetype": "audio/wav",
                "filename": "voice-message.wav",
                "data": base64.b64encode(audio_bytes).decode("ascii"),
            },
            "convert": True,
        }
        if reply_to:
            body["reply_to"] = reply_to
        resp = await self._client.post("/api/sendVoice", json=body)
        resp.raise_for_status()
        return resp.json()
