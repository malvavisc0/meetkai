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

import asyncio
import json
import logging
from email.message import EmailMessage
from pathlib import Path
from typing import Literal

import httpx
import uvicorn
from pydantic import Field
from rich.console import Console

from kai.agent.context import MessageContext
from kai.agent.core import ActionResult, ChatResult, KaiAgent
from kai.agent.tools import WEB_WORKFLOW_INSTRUCTIONS
from kai.agent.tools.conversation import register_conversation_tools
from kai.agent.tools.email import (
    SmtpSettings,
    _valid_recipient,
    format_from_header,
    get_smtp_settings,
    send_via_smtp,
    set_email_body,
)
from kai.bots.base import BaseBot, TellResult
from kai.bots.email.config import EmailSettings, get_email_settings
from kai.bots.email.setup import BotConfig
from kai.bots.processing import PostProcessor
from kai.bots.waha.webhook import create_webhook_app
from kai.config.filters import should_process_chat_message
from kai.config.prompts import load_system_prompt
from kai.config.settings import Settings
from kai.templates.resolver import ToolResolution, resolve_config
from kai.templates.schema import PostProcessingConfig, TemplateDef

logger = logging.getLogger(__name__)
console = Console()


class EmailAction(ActionResult):
    """Action vocabulary for the email support bot.

    - ``reply``    — send ``text`` as an email to ``target`` (the recipient
                     address). On an inbound turn ``target`` is the sender;
                     on an operator turn it's whichever address the operator
                     named in the instruction.
    - ``console``  — operator turns only: don't send an email, just return
                     ``text`` to the operator's console (answering a question
                     the operator asked, confirming a directive).
    - ``silent``   — don't reply at all (automated mail, spam, empty content).

    No ``send_voice_note``/``sleep``/``send_dm``/``send_to_group`` — the
    framework's ``agent.chat`` accepts any ``ActionResult`` subclass as
    ``output_cls``; this is the intended extension point.
    """

    action: Literal["reply", "console", "silent"] = Field(  # type: ignore[assignment]
        description=(
            "'reply' to send an email back (fill `text` with the full message "
            "body and `target` with the recipient address). This is the "
            "default for any genuine question, even a short or hard one. "
            "'console' is operator-only: answer the operator directly without "
            "sending any email. 'silent' ONLY for content-free connectivity "
            "tests, automated/system-generated mail (out-of-office, bounces, "
            "calendar responses, unsubscribe confirmations), pure spam, or "
            "empty/unreadable content — never silent just because a question "
            "is ambiguous or you don't know the answer; say so in a reply "
            "instead."
        )
    )
    text: str | None = None
    target: str | None = Field(
        default=None,
        description=(
            "Recipient email address for 'reply'. On an inbound turn this is "
            "the sender's address. On an operator turn, copy the exact "
            "address from the instruction (never invent or guess one). Leave "
            "empty for 'console' and 'silent'."
        ),
    )


class Bot(BaseBot):
    name = "email"

    def __init__(self, bot_dir: Path, config: BotConfig | None = None):
        super().__init__(bot_dir)
        self._agent: KaiAgent | None = None
        self._settings: Settings | None = None
        self._email: EmailSettings | None = None
        self._config: BotConfig = config or BotConfig()
        self._prompt: str = ""
        self._reply_style: str = ""
        self._post_processor = PostProcessor(PostProcessingConfig(profile="none"))
        self._server: uvicorn.Server | None = None
        self._shutting_down = asyncio.Event()
        self._smtp: SmtpSettings | None = None

    def configure(
        self,
        agent: KaiAgent,
        settings: Settings,
        *,
        voice: str | None = None,
        template: TemplateDef,
        tools: ToolResolution,
    ) -> None:
        super().configure(agent, settings, voice=voice, template=template, tools=tools)
        self._settings = settings
        self._email = get_email_settings()
        self._config = self._load_config(template)
        if settings.agent_language_explicit:
            self._config = self._config.model_copy(update={"language": settings.agent_language})
        self._reply_style = template.reply_style
        self._post_processor = PostProcessor(template.post_processing)
        self._prompt = self._load_prompt(template)
        agent.set_system_prompt(self._prompt)
        agent.set_temperature(self._config.temperature)
        agent.set_timezone(self._config.timezone)
        if self._has_tool("web_search"):
            agent.add_tool_workflow(WEB_WORKFLOW_INSTRUCTIONS)
        if self._has_tool("record_note") or self._has_tool("get_conversation_messages"):
            register_conversation_tools(agent, tool_context=self._tool_context)
        self.setup_task_scheduler(agent, settings)
        # Wire escalation and blacklist tools' module-level state to this bot.
        self._wire_escalation_tools(settings, self._config.blacklist)
        # SMTP settings for the reply path (KAI_SMTP_TOOL_* env, injected by
        # the cockpit's required-credential env-injection loop added in 01).
        self._smtp = get_smtp_settings()

    def _load_config(self, template: TemplateDef, config_path: Path | None = None) -> BotConfig:
        # Config merge: BotConfig defaults ← template.config ← config.json
        # (the per-deployment file the cockpit writes).
        path = config_path or self.resolve_config_path()
        config_file_data: dict | None = None
        if path is not None and path.exists():
            try:
                config_file_data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in bot config {path}: {exc}") from exc
        return resolve_config(template, config_file_data, {}, BotConfig)

    def display_name(self) -> str:
        return self._config.display_name

    def _load_prompt(self, template: TemplateDef) -> str:
        from kai.templates import TemplateRegistry, escalation_prompt_section

        registry = TemplateRegistry.bundled()
        path = registry.prompt_path(template.transport, template.name)
        if path is None:
            path = self.bot_dir / "prompt.md"
        prompt = load_system_prompt(
            str(path),
            variables={
                "language": self._config.language,
                "display_name": self._config.display_name,
            },
        )
        # Escalation rules are appended to the base prompt (system-prompt
        # step 1) so they read as hard rules before tool instructions.
        return prompt + escalation_prompt_section(template)

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
        # turn — the list is re-checked from config on every email (no
        # persisted block history). Both args are the normalized sender;
        # case-insensitive comparison catches variations like "Alice@Ex.com".
        if not should_process_chat_message(
            source.strip().lower(),
            source.strip().lower(),
            set(),
            {e.lower() for e in self._config.blacklist},
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
                reply_style=self._reply_style or None,
            )

            action = result.action
            if action.action == "reply" and action.text and not self._sent_via_tool(result, source):
                await self._send_reply(source, subject, action.text)
            return {"ok": True}
        except Exception:
            logger.exception("ingest_event failed for %s", event.get("source", "<unknown>"))
            return {"ok": False}

    @staticmethod
    def _sent_via_tool(result: ChatResult, target: str) -> bool:
        """True if a ``send_email`` tool call already delivered to ``target``.

        The generic ``send_email`` tool (``kai.agent.tools.email``) is
        registered on every bot's agent whenever SMTP is configured — the
        email bot included — alongside this bot's own action-based send
        path (``reply`` -> :meth:`_send_reply`). Nothing stops the model
        from using both in the same turn (e.g. calling the tool, then still
        returning ``action="reply"`` in its structured output), which would
        otherwise deliver the same message twice. Checking the turn's
        recorded tool calls here — rather than trying to prompt the model
        out of ever using the tool — is what actually prevents the double
        send regardless of which path the model picks.
        """
        target_norm = target.strip().lower()
        for tc in result.tool_calls:
            if tc.name != "send_email" or not tc.ok:
                continue
            to = str(tc.args.get("to", "")).strip().lower()
            if to == target_norm:
                return True
        return False

    _OPERATOR_TURN_CONTEXT = (
        "You received an instruction from the operator (the person who runs "
        "you). Express your decision through the structured action object "
        "you return.\n"
        "IMPORTANT: action values (reply, console, silent) are NOT tools. "
        'Never call them as functions. They are values for the "action" '
        "field in your JSON response.\n"
        '- To send an email to someone, set action to "reply" with BOTH '
        '"target" = the exact email address taken from the instruction '
        '(never invent or guess one) and "text" = the full email body to '
        "send (plain prose, no action tokens or field names in it). "
        'Returning a reply with an empty "target" or "text" is never '
        "correct — if the instruction gives you both, copy them verbatim "
        "into the fields. You may also have a send_email tool available; "
        "if you use it to deliver the message, still return action "
        '"reply" with the same target/text so the send is recorded — the '
        "framework detects the tool already delivered it and will not send "
        "a second copy, so you never need to worry about double-sending.\n"
        "- To answer the operator ONLY (answer a question the operator "
        "asked you directly, confirm a steering directive), set action to "
        '"console" and put your reply in "text". The console reply goes to '
        "the operator's chat interface, NOT to any email — the recipient "
        "never sees it.\n"
        "CRITICAL: when the instruction tells you to email, reply, send, or "
        "respond to a specific address (e.g. 'send an email to "
        "alice@example.com', 'reply to bob'), you MUST use action \"reply\" "
        "with that address as the target — NEVER console. console is only "
        "for when the operator themselves is asking you a question and "
        "wants the answer back in this console, not delivered by email. If "
        "the instruction mentions an email address and asks you to say "
        "something there, the answer is always a reply action, not console."
    )

    async def handle_operator(self, message: str, *, persist: bool = False) -> TellResult:
        """Run an operator turn under the isolated ``operator`` history bucket.

        Mirrors the waha bot's operator console: the agent decides what to do
        (send an email, answer the operator, stay silent) through its
        structured ``EmailAction``. The bot dispatches it — see
        :meth:`_sent_via_tool` for why a tool call and a ``reply`` action in
        the same turn don't cause a double send. ``reply`` delivers the
        email via SMTP (unless already delivered by the tool) and records it
        in the target's history; ``console`` returns the reply to the
        operator only; ``silent`` is a no-op.
        """
        if self._agent is None:
            return TellResult(ok=False, reply="bot has no agent")

        console.print(f"[magenta]< operator[/magenta]  {message}")

        try:
            result = await self._agent.chat(
                message,
                output_cls=EmailAction,
                conversation_id="operator",
                context=MessageContext(
                    sender_name="operator",
                    sender_id="<operator>",
                    addressed_to_bot=True,
                ),
                extra_system_context=self._OPERATOR_TURN_CONTEXT,
                # ``reply`` text is addressed to the *target* email, not the
                # operator — don't record it as an assistant reply in the
                # operator's own history bucket. ``_dispatch_operator_action``
                # records it in the target's history once delivery is confirmed.
                is_delegated_action=lambda a: a.action == "reply",
            )
        except Exception:
            logger.exception("operator turn failed")
            return TellResult(ok=False, reply="operator turn failed")

        return await self._dispatch_operator_action(result)

    async def _dispatch_operator_action(self, result: ChatResult) -> TellResult:
        """Dispatch the agent's structured action for an operator turn.

        ``reply`` sends an email to ``action.target`` via SMTP and records it
        in that address's conversation history (mirroring the inbound path);
        ``console`` returns the reply text to the operator verbatim; ``silent``
        is a no-op.
        """
        action = result.action
        kind = action.action

        if kind == "reply":
            reply = ""
            target = (action.target or "").strip()
            out_text = action.text or ""
            sent_ok = True
            if target and out_text:
                already_sent = self._sent_via_tool(result, target)
                if already_sent:
                    console.print(
                        f"[green]>[/green]  {out_text[:60]}  "
                        f"[dim](sent via send_email tool to {target})[/dim]"
                    )
                else:
                    console.print(
                        f"[green]>[/green]  {out_text[:60]}  [dim](email to {target})[/dim]"
                    )
                    try:
                        await self._send_reply(target, "", out_text)
                    except Exception as exc:
                        logger.error("Failed to send email to %s: %s", target, exc)
                        console.print(f"[red]send failed: {target}: {exc}[/red]")
                        sent_ok = False
                if sent_ok and self._agent is not None:
                    await self._agent.record_assistant_message(target, out_text)
                snippet = out_text if len(out_text) <= 60 else out_text[:57] + "..."
                reply = f"sent to {target}: {snippet}" if sent_ok else f"failed to send to {target}"
            else:
                if not target:
                    sent_ok = False
                    reply = "reply action missing target"
                elif not out_text:
                    sent_ok = False
                    reply = "reply action missing text"
            return TellResult(
                ok=sent_ok and result.error is None,
                actions=[{"tool": "send_reply", "target": target, "text": out_text, "ok": sent_ok}],
                reply=reply,
            )

        if kind == "silent":
            return TellResult(ok=True, reply="", actions=[{"tool": kind, "ok": True}])

        actions: list[dict] = []
        for tc in result.tool_calls:
            entry: dict = {"tool": tc.name, "ok": tc.ok}
            for key in ("target", "text"):
                if key in tc.args:
                    value = tc.args[key]
                    if isinstance(value, str) and len(value) > 200:
                        value = value[:197] + "..."
                    entry[key] = value
            actions.append(entry)
        reply = action.text or ""
        ok = result.error is None
        if result.error:
            reply = f"error: {result.error}"
        return TellResult(ok=ok, actions=actions, reply=reply)

    async def status_snapshot(self) -> dict:
        return {
            "bot": "email",
            "language": self._config.language,
            "capabilities": {"vision": self._config.vision},
        }

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

        # Apply the template's post-processing pipeline. ``general`` uses
        # ``profile: none`` (identity — email supports markdown), so this is a
        # no-op for the default; an email template that sets ``profile: custom``
        # gets its transforms applied before SMTP send.
        body = self._post_processor.process(body)

        msg = EmailMessage()
        msg["Subject"] = f"Re: {subject}" if subject else "Re: your email"
        msg["From"] = format_from_header(self._config.display_name, self._smtp.from_address)
        msg["To"] = to
        set_email_body(msg, body)

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

        Sourced from ``BotConfig.vision`` (config.json, written by the cockpit
        from ``Deployment.feature_flags["image"]``) — same channel waha uses
        for ``media.image_enabled``.
        """
        return self._config.vision


__all__ = ["Bot", "BotConfig", "EmailAction"]
