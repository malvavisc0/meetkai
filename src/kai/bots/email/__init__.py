"""Email support bot — receives inbound emails via the cockpit's /ingest
forward, runs an agent turn, and replies via SMTP.

Modeled on ``bots/waha/__init__.py`` but stripped of every WAHA/STT/TTS/
media/group/sleep/participation concern. The provider (Resend) never POSTs
directly to this bot — the cockpit verifies the webhook, normalizes it to a
``NormalizedMessage``, and forwards to this bot's ``/ingest``. The bot's own
uvicorn server serves only the operator surfaces (``/ingest``, ``/tell``,
``/status``, ``/clear``) and verifies each with the HMAC key injected as
``KAI_BOT_HMAC_KEY``.

Conversation history is the framework's built-in JSON history keyed by
``conversation_id`` (the sender's email) — there is no transport-specific
history tool here (unlike the waha bot's WAHA-fetching ``get_chat_history``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from email.message import EmailMessage
from pathlib import Path
from typing import Literal

import httpx
import uvicorn
from pydantic import Field

from kai.agent.context import MessageContext
from kai.agent.core import ActionResult, ChatResult, KaiAgent
from kai.agent.tools import WEB_WORKFLOW_INSTRUCTIONS
from kai.agent.tools.email import (
    DEFAULT_DISPLAY_NAME,
    SmtpSettings,
    _valid_recipient,
    format_from_header,
    get_smtp_settings,
    send_via_smtp,
)
from kai.bots.base import BaseBot, TellResult
from kai.bots.email.config import EmailSettings, get_email_settings
from kai.bots.email.setup import BotConfig
from kai.bots.waha.webhook import create_webhook_app
from kai.config.filters import should_process_chat_message
from kai.config.prompts import load_system_prompt
from kai.config.settings import Settings

logger = logging.getLogger(__name__)


def _parse_email_list(raw: object) -> list[str]:
    """Parse a config.json list field into normalized email addresses.

    Mirrors ``bots/waha/__init__.py``'s ``_parse_id_list`` helper. Entries
    are lowercased and stripped so blacklist comparisons in ``ingest_event``
    are case-insensitive (email addresses are conventionally compared
    case-insensitively, unlike WAHA's chat/author IDs).
    """
    if not isinstance(raw, list):
        if raw:
            logger.warning(
                "config.json blacklist must be a list, got %s — ignoring", type(raw).__name__
            )
        return []
    return [str(entry).strip().lower() for entry in raw if isinstance(entry, str) and entry.strip()]


class EmailAction(ActionResult):
    """Action vocabulary for the email support bot.

    One reply target (the sender) and one decision (reply or stay silent).
    No ``send_to_group``/``send_dm``/``send_voice_note``/``sleep`` — the
    framework's ``agent.chat`` accepts any ``ActionResult`` subclass as
    ``output_cls``; this is the intended extension point.
    """

    action: Literal["reply", "silent"] = Field(  # type: ignore[assignment]
        description=(
            "'reply' to send an email back to the sender (fill `text` with "
            "the full message body); this is the default for any genuine "
            "question, even a short or hard one. 'silent' ONLY for "
            "content-free connectivity tests, automated/system-generated "
            "mail (out-of-office, bounces, calendar responses, "
            "unsubscribe confirmations), pure spam, or empty/unreadable "
            "content — never silent just because a question is ambiguous "
            "or you don't know the answer; say so in a reply instead."
        )
    )
    text: str | None = None


class Bot(BaseBot):
    name = "email"

    def __init__(self, bot_dir: Path, config: BotConfig | None = None):
        super().__init__(bot_dir)
        self._agent: KaiAgent | None = None
        self._settings: Settings | None = None
        self._email: EmailSettings | None = None
        self._config: BotConfig = config or BotConfig()
        self._prompt: str = ""
        self._server: uvicorn.Server | None = None
        self._shutting_down = asyncio.Event()
        self._smtp: SmtpSettings | None = None

    def configure(self, agent: KaiAgent, settings: Settings, *, voice: str | None = None) -> None:
        self._agent = agent
        self._settings = settings
        self._email = get_email_settings()
        self._config = self._load_config()
        if settings.agent_language_explicit:
            self._config = self._config.model_copy(update={"language": settings.agent_language})
        self._prompt = self._load_prompt()
        agent.set_system_prompt(self._prompt)
        agent.set_temperature(self._config.temperature)
        agent.set_timezone(self._config.timezone)
        # The default tool set (web_search / get_webpage_content, registered
        # for every KaiAgent by get_tools()) needs the same fetch-before-cite
        # / cross-check-sources protocol the waha bot gets via this same
        # constant — without it the model has tools but no usage guidance.
        agent.set_tool_workflow(WEB_WORKFLOW_INSTRUCTIONS)
        self.setup_task_scheduler(agent, settings)
        # SMTP settings for the reply path (KAI_SMTP_TOOL_* env, injected by
        # the cockpit's required-credential env-injection loop added in 01).
        self._smtp = get_smtp_settings()

    def _load_config(self, config_path: Path | None = None) -> BotConfig:
        path = config_path or self.resolve_config_path()
        if path is None or not path.exists():
            return BotConfig()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in bot config {path}: {exc}") from exc
        return BotConfig(
            language=str(data.get("language", "English")),
            timezone=str(data.get("timezone", "")).strip() or None,
            temperature=float(data.get("temperature", 0.2)),
            blacklist=_parse_email_list(data.get("blacklist")),
            display_name=str(data.get("display_name", "")).strip() or DEFAULT_DISPLAY_NAME,
        )

    def display_name(self) -> str:
        return self._config.display_name

    def _load_prompt(self) -> str:
        return load_system_prompt(
            str(self.bot_dir / "prompt.md"),
            variables={"language": self._config.language},
        )

    async def run(self) -> None:
        assert self._email is not None, "configure() must be called before run()"
        self._shutting_down.clear()
        self.start_task_scheduler()

        app = create_webhook_app(
            hmac_key=self._email.hmac_key,
            hmac_algorithm=self._email.hmac_algorithm,
            webhook_path="/webhook/email",
            on_message=None,
            on_tell=self.handle_operator,
            on_ingest=self.ingest_event,
            on_status=self.status_snapshot,
            on_clear=self.clear_operator_history,
        )
        config = uvicorn.Config(
            app,
            host=self._email.control_host,
            port=self._email.control_port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)
        await self._server.serve()

    def tell_endpoint(self) -> str | None:
        if self._email is None:
            return None
        host = self._email.control_host
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return f"http://{host}:{self._email.control_port}"

    def tell_hmac_key(self) -> str | None:
        return self._email.hmac_key if self._email is not None else None

    def tell_hmac_algorithm(self) -> str:
        return self._email.hmac_algorithm if self._email is not None else "sha512"

    async def stop(self) -> None:
        await super().stop()
        if self._server and not self._shutting_down.is_set():
            self._shutting_down.set()
            self._server.should_exit = True

    async def ingest_event(self, event: dict) -> dict:
        """Receive a forwarded, already-normalized inbound email event.

        Overrides ``BaseBot.ingest_event``. The ``event`` dict is the
        ``model_dump()`` of a ``NormalizedMessage`` (see its contract):
        ``source`` (sender email), ``text`` (body), ``metadata`` (subject,
        message_id, to, attachments), ``event`` (``email.inbound``).
        """
        if event.get("event") != "email.inbound":
            return {"ok": False}

        source = event.get("source", "")
        # Drop blacklisted senders before any attachment download or agent
        # turn — no block history is persisted, the list is re-checked from
        # config on every inbound email. should_process_chat_message treats
        # a bare email address the same as a non-group chat_id (no "@g.us"),
        # so passing it as both chat_id and author with an empty whitelist
        # reduces to a plain blacklist membership check. Compare
        # case-insensitively, matching the lowercased entries loaded in
        # _load_config.
        if not should_process_chat_message(
            source.strip().lower(), source.strip().lower(), set(), set(self._config.blacklist)
        ):
            logger.info("Ignoring blacklisted sender: %s", source)
            return {"ok": True}

        try:
            assert self._agent is not None
            text = event["text"]
            metadata = event.get("metadata", {})
            subject = metadata.get("subject", "")
            attachments = metadata.get("attachments", [])

            images, enriched_text = await self._process_attachments(text, attachments)

            context = MessageContext(
                sender_name=source,
                sender_id=source,
                conversation_id=source,
                multi_party=False,
                addressed_to_bot=True,
            )

            self.set_task_context(chat_id=source, owner_id=source, tz_hint=self._config.timezone)

            result = await self._agent.chat(
                enriched_text,
                output_cls=EmailAction,
                conversation_id=source,
                context=context,
                images=images or None,
            )

            action = result.action
            if action.action == "reply" and action.text:
                await self._send_reply(source, subject, action.text)
            return {"ok": True}
        except Exception:
            logger.exception("ingest_event failed for %s", event.get("source", "<unknown>"))
            return {"ok": False}

    async def handle_operator(
        self, message: str, *, persist: bool = False, to: str = ""
    ) -> TellResult:
        """Run a console turn under the isolated ``operator`` history bucket.

        Mirrors the waha bot's console send parity: when the model decides
        to reply *and* the operator supplied a real ``to`` address (a
        "send as real email to" field in the console UI, never inferred),
        the reply is actually emailed and recorded in that address's own
        history bucket — exactly like a genuine inbound-email round trip —
        in addition to the normal ``operator`` transcript. Omitting ``to``
        keeps the turn local/no-send, as before.
        """
        if self._agent is None:
            return TellResult(ok=False, reply="bot has no agent")

        result: ChatResult = await self._agent.chat(
            message,
            output_cls=EmailAction,
            conversation_id="operator",
            context=MessageContext(
                sender_name="operator",
                sender_id="<operator>",
                addressed_to_bot=True,
            ),
        )
        action = result.action
        reply = action.text or ""

        to_addr = to.strip()
        if action.action == "reply" and to_addr and action.text:
            try:
                await self._send_reply(to_addr, "", action.text)
            except Exception as exc:
                logger.error("Console send to %s failed: %s", to_addr, exc)
                return TellResult(ok=False, reply=f"{reply}\n\n(send to {to_addr} failed: {exc})")
            await self._agent.record_assistant_message(to_addr, action.text)
            # An explicit action entry, not just the ``ok`` flag, lets the
            # cockpit confirm a real send happened (mirroring WahaAction's
            # send_to_group/send_dm actions). A bot process still running
            # pre-``to``-aware code (restart-pending) has no way to
            # populate this — it silently ignores the unrecognized field —
            # so its response carries no confirmation entry, and the
            # console correctly shows no "sent" claim instead of a false one.
            return TellResult(
                ok=True,
                actions=[
                    {"tool": "send_reply", "target": to_addr, "text": action.text, "ok": True}
                ],
                reply=reply,
            )

        return TellResult(ok=True, reply=reply)

    async def status_snapshot(self) -> dict:
        return {"bot": "email", "language": self._config.language}

    async def clear_operator_history(self) -> dict:
        if self._agent is None:
            return {"ok": False, "error": "bot has no agent"}
        await self._agent.clear_history("operator")
        return {"ok": True}

    async def _send_reply(self, to: str, subject: str, body: str) -> None:
        """Send a reply email via the operator's SMTP account.

        Reuses ``SmtpSettings`` (``KAI_SMTP_TOOL_*`` env) and
        ``_valid_recipient`` from ``agent/tools/email.py`` — same ``smtplib``
        pattern as ``make_send_email_tool``. The ``from`` address is closed
        over from env; the LLM cannot override it (spoofing guard).
        """
        if not self._smtp or not self._smtp.smtp_enabled:
            logger.error("SMTP not configured — cannot reply to %s", to)
            raise RuntimeError("SMTP not configured")

        if not _valid_recipient(to):
            logger.error("Invalid recipient: %s", to)
            raise RuntimeError(f"invalid recipient: {to}")

        msg = EmailMessage()
        msg["Subject"] = f"Re: {subject}" if subject else "Re: your email"
        msg["From"] = format_from_header(self._config.display_name, self._smtp.from_address)
        msg["To"] = to
        msg.set_content(body)

        send_via_smtp(
            msg,
            host=self._smtp.host,
            port=int(self._smtp.port),
            username=self._smtp.username,
            password=self._smtp.password,
            use_tls=self._smtp.use_tls,
        )

    async def _process_attachments(
        self, text: str, attachments: list[dict]
    ) -> tuple[list[bytes], str]:
        """Download image attachments, tag non-image attachments.

        Returns ``(image_bytes_list, enriched_text)``. Image bytes are only
        downloaded when vision is enabled; otherwise (or for non-images) a
        ``[attachment: ...]`` text tag is injected — the same graceful
        degradation the waha bot uses when ``image_enabled=False``.
        """
        images: list[bytes] = []
        enriched = text

        for att in attachments:
            content_type = att.get("content_type", "")
            filename = att.get("filename", "")
            url = att.get("url", "")

            if content_type.startswith("image/") and self._vision_enabled():
                img_bytes = await self._download_attachment(url)
                if img_bytes:
                    images.append(img_bytes)
                    tag = f"[image attached: {filename}]"
                else:
                    tag = f"[image attachment failed to download: {filename}]"
            else:
                tag = f"[attachment: {filename} ({content_type})]"

            enriched = f"{tag}\n{enriched}" if enriched else tag

        return images, enriched

    async def _download_attachment(self, url: str) -> bytes | None:
        """Download an attachment with a size cap.

        The URL comes from Resend's signed webhook (already verified by the
        cockpit's ingress route), so it's trusted as a source — but the
        content is still untrusted user data (prompt-injection guard in
        ``prompt.md`` covers this).

        Uses ``httpx.AsyncClient`` so the bot's event loop isn't blocked
        during the download (other /ingest, /tell, /status requests keep
        flowing while bytes are fetched).
        """
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                if self._email is None:
                    return None
                if len(resp.content) > self._email.max_attachment_bytes:
                    logger.warning("Attachment too large: %d bytes", len(resp.content))
                    return None
                return resp.content
        except Exception:
            logger.exception("Failed to download attachment: %s", url)
            return None

    def _vision_enabled(self) -> bool:
        """True when the deployment's image feature flag is on.

        Sourced from ``EmailSettings.vision`` (``KAI_EMAIL_VISION``), injected
        by the cockpit at start time from ``Deployment.feature_flags["image"]``.
        """
        return self._email is not None and self._email.vision


__all__ = ["Bot", "BotConfig", "EmailAction"]
