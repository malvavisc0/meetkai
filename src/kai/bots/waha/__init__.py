import asyncio
import json
import logging
import re
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from rich.console import Console

from kai.agent.context import MessageContext
from kai.agent.core import KaiAgent, is_silent_reply, strip_reasoning_channels
from kai.agent.tools import WEB_WORKFLOW_INSTRUCTIONS
from kai.bots.base import BaseBot
from kai.bots.waha.client import WahaClient
from kai.bots.waha.config import WahaSettings
from kai.bots.waha.history import register_chat_history_tool
from kai.bots.waha.instagram import extract_instagram_shortcode, fetch_instagram_post
from kai.bots.waha.media import MediaAttachment, MediaType, extract_media
from kai.bots.waha.mentions import resolve_inbound_mentions, resolve_mentions, strip_mention_markup
from kai.bots.waha.payload import GROUP_SUFFIX, MessageMetadata, _sanitize_name, parse_message
from kai.bots.waha.processing import (
    DEFAULT_SLEEP_ACK,
    REPLY_STYLE,
    has_sleep_token,
    has_tool_call_leak,
    looks_like_base64_media,
    post_process,
    should_organically_participate,
    strip_sleep_token,
)
from kai.bots.waha.seen_store import SeenStore
from kai.bots.waha.setup import (
    _DEFAULT_PARTICIPATION_COOLDOWN,
    _DEFAULT_PARTICIPATION_RATE,
    _DEFAULT_PARTICIPATION_STREAK_MAX,
    BotConfig,
    MediaConfig,
    ParticipationConfig,
)
from kai.bots.waha.sleep_store import SleepStore
from kai.bots.waha.stt import STTProvider, create_stt_provider, resolve_whisper_language
from kai.bots.waha.webhook import create_webhook_app
from kai.cli import BotStartupError
from kai.config.filters import should_process_chat_message
from kai.config.prompts import load_system_prompt
from kai.config.settings import Settings
from kai.utils.terminal import render_image_pixelated

logger = logging.getLogger(__name__)
console = Console()

_SEND_MAX_RETRIES = 3
_SEND_RETRY_BASE_DELAY = 1.0
_SEEN_IDS_MAX = 2048
_ROSTER_MAX_CHATS = 1024
_CHAT_LOCKS_MAX = 1024
# Group participant lists are refreshed on inbound group messages but cached
# per chat so we don't hammer WAHA on every message in a fast chat.
_ROSTER_REFRESH_TTL = 300.0

# Re-export public API so ``from kai.bots.waha import ...`` keeps working.
__all__ = [
    "Bot",
    "BotConfig",
    "MediaConfig",
    "ParticipationConfig",
    "_build_webhook_url",
]


def _build_webhook_url(public_host: str, webhook_path: str) -> str:
    parsed = urlparse(public_host)
    if parsed.scheme:
        return f"{parsed.scheme}://{parsed.netloc}{webhook_path}"
    return f"http://{public_host}{webhook_path}"


_URL_TRAILING_PUNCT = ".,;:!?\"'"
_URL_CLOSE_OPEN = {")": "(", "]": "[", "}": "{", ">": "<"}


def _strip_unbalanced_trailing_punct(url: str) -> str:
    """Strip trailing punctuation captured greedily by ``\\S+``.

    Only removes closers that are unbalanced relative to their matching opener
    in the URL, so a real URL such as
    ``https://en.wikipedia.org/wiki/Foo_(bar)`` keeps its closing ``)``.
    """
    while url:
        ch = url[-1]
        if ch in _URL_TRAILING_PUNCT:
            url = url[:-1]
            continue
        if ch in _URL_CLOSE_OPEN:
            opener = _URL_CLOSE_OPEN[ch]
            # Strip the closer only while it has no matching opener to its left.
            if url.count(opener) < url.count(ch):
                url = url[:-1]
                continue
        break
    return url


def _bounded_dict_set(d: OrderedDict, key, value, max_size: int) -> None:
    if key in d:
        d.move_to_end(key)
    d[key] = value
    while len(d) > max_size:
        d.popitem(last=False)


class Bot(BaseBot):
    name = "waha"

    def __init__(self, bot_dir: Path, config: BotConfig | None = None):
        super().__init__(bot_dir)
        self._agent: KaiAgent | None = None
        self._settings: Settings | None = None
        self._waha: WahaSettings | None = None
        self._config: BotConfig = config or BotConfig()
        self._prompt: str = ""
        self._bot_ids: set[str] = set()
        self._rosters: OrderedDict[str, dict[str, str]] = OrderedDict()
        self._roster_refreshed_at: dict[str, float] = {}
        self._group_admins: dict[str, set[str]] = {}
        self._chat_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._sleep_store: SleepStore | None = None
        self._last_reply_at: dict[str, float] = {}
        self._consecutive_replies: dict[str, int] = {}
        self._server: uvicorn.Server | None = None
        self._shutting_down = asyncio.Event()
        self._stt: STTProvider | None = None
        self._stt_language: str = "auto"
        self._waha_client: WahaClient | None = None
        self._seen_store: SeenStore | None = None
        self._vibe_msg_count = 0
        self._vibe_interval = 25

    def configure(self, agent: KaiAgent, settings: Settings) -> None:
        self._agent = agent
        self._settings = settings
        self._waha = WahaSettings()
        self._config = self._load_config()
        if settings.agent_language_explicit:
            self._config.language = settings.agent_language
        self._prompt = self._load_prompt()
        agent.set_system_prompt(self._prompt)
        agent.set_tool_workflow(WEB_WORKFLOW_INSTRUCTIONS)
        agent.set_tool_call_callback(self._render_tool_call)
        agent.set_timezone(self._config.timezone)
        self.setup_task_scheduler(agent, settings)
        register_chat_history_tool(agent, bot=self)
        self._seen_store = SeenStore(self._seen_store_path(settings), max_size=_SEEN_IDS_MAX)
        self._sleep_store = SleepStore(self._sleep_store_path(settings))
        if self._config.media.voice_enabled:
            # The whisper language and the chat language are separate settings.
            # When the user passes --language explicitly, use it for STT too,
            # unless KAI_WAHA_WHISPER_LANGUAGE was set to something other than
            # "auto" (its default) — i.e. an explicit whisper override wins.
            whisper_lang = self._waha.whisper_language
            if settings.agent_language_explicit and whisper_lang == "auto":
                whisper_lang = resolve_whisper_language(settings.agent_language)
            self._stt = create_stt_provider(
                ffmpeg_path=self._waha.ffmpeg_path,
                whisper_cpp_path=self._waha.whisper_cpp_path,
                model_path=self._waha.whisper_model_path,
                language=whisper_lang,
                server_mode=self._waha.whisper_server_mode,
                server_host=self._waha.whisper_server_host,
                server_port=self._waha.whisper_server_port,
                server_threads=self._waha.whisper_server_threads,
            )
            self._stt_language = whisper_lang

    def _load_config(self, config_path: Path | None = None) -> BotConfig:
        path = config_path or self.resolve_config_path()
        if path is None or not path.exists():
            raise FileNotFoundError(
                f"No bot config found for '{self.name}'. Looked for "
                f"external override and packaged default in {self.bot_dir}."
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in bot config {path}: {exc}") from exc

        def _parse_id_list(raw: object, field_name: str) -> list[str]:
            if not isinstance(raw, list):
                if raw:
                    logger.warning(
                        "config.json %s must be a list, got %s — ignoring",
                        field_name,
                        type(raw).__name__,
                    )
                return []
            return [str(entry).strip() for entry in raw if isinstance(entry, str) and entry.strip()]

        return BotConfig(
            trigger_keyword=str(data.get("trigger_keyword", "kai")).strip(),
            whitelist=_parse_id_list(data.get("whitelist"), "whitelist"),
            blacklist=_parse_id_list(data.get("blacklist"), "blacklist"),
            language=str(data.get("language", "English")),
            timezone=str(data.get("timezone", "")).strip() or None,
            mentions_enabled=bool(data.get("mentions_enabled", True)),
            media=MediaConfig(
                image_enabled=bool(data.get("media", {}).get("image_enabled", True)),
                voice_enabled=bool(data.get("media", {}).get("voice_enabled", True)),
                instagram_enabled=bool(data.get("media", {}).get("instagram_enabled", True)),
                max_size_mb=int(data.get("media", {}).get("max_size_mb", 10)),
            ),
            participation=ParticipationConfig(
                enabled=bool(data.get("participation", {}).get("enabled", True)),
                rate=float(data.get("participation", {}).get("rate", _DEFAULT_PARTICIPATION_RATE)),
                cooldown_seconds=float(
                    data.get("participation", {}).get(
                        "cooldown_seconds", _DEFAULT_PARTICIPATION_COOLDOWN
                    )
                ),
                streak_max=int(
                    data.get("participation", {}).get(
                        "streak_max", _DEFAULT_PARTICIPATION_STREAK_MAX
                    )
                ),
            ),
        )

    def _load_prompt(self) -> str:
        return load_system_prompt(
            str(self.bot_dir / "prompt.md"),
            variables={"language": self._config.language},
        )

    async def run(self) -> None:
        self._shutting_down.clear()
        self.start_task_scheduler()

        if self._stt:
            await self._stt.start()

        from kai.agent.tools.hardware import epaper_available, epaper_clear

        if epaper_available():
            logger.info("clearing e-Paper display on boot")
            epaper_clear()

        self._print_startup_config()

        try:
            await self._connect_waha()
        except SystemExit as exc:
            raise BotStartupError(str(exc)) from exc

        self._waha_client = WahaClient(self._waha)

        app = create_webhook_app(
            hmac_key=self._waha.hmac_key,
            hmac_algorithm=self._waha.hmac_algorithm,
            webhook_path=self._waha.webhook_path,
            on_message=self._handle_message,
        )
        config = uvicorn.Config(
            app,
            host=self._waha.webhook_host,
            port=self._waha.webhook_port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)

        if self._shutting_down.is_set():
            return
        await self._server.serve()

    async def stop(self) -> None:
        await super().stop()
        if self._stt:
            await self._stt.stop()
        if self._waha_client:
            await self._waha_client.close()
            self._waha_client = None

        from kai.agent.tools.hardware import epaper_available, epaper_sleep

        if epaper_available():
            logger.info("putting e-Paper display to sleep")
            epaper_sleep()

        if self._server and not self._shutting_down.is_set():
            self._shutting_down.set()
            self._server.should_exit = True

    def _epaper_available(self) -> bool:
        from kai.agent.tools.hardware import epaper_available

        return epaper_available()

    def _epaper_sleep_screen(self) -> None:
        if not self._epaper_available():
            return
        from kai.agent.tools.hardware import render_sleep_screen

        result = render_sleep_screen()
        logger.info("epaper sleep screen: %s", result)

    def _epaper_wake_screen(self) -> None:
        if not self._epaper_available():
            return
        from kai.agent.tools.hardware import render_wake_screen

        result = render_wake_screen()
        logger.info("epaper wake screen: %s", result)

    async def _run_vibe_check(self, chat_id: str) -> None:
        """Assess the chat's vibe and render it to the e-Paper display."""
        if not self._epaper_available():
            return
        try:
            messages = self._agent._get_history(chat_id)[-15:]
            if not messages:
                return

            transcript = "\n".join(
                f"{m.role.value if hasattr(m.role, 'value') else m.role}: {m.content}"
                for m in messages
                if m.content
            )
            if not transcript.strip():
                return

            prompt = (
                "You are a vibe analyzer. Read this group chat excerpt and "
                "rate the energy on a scale of 0-100.\n\n"
                "Respond in EXACTLY this format, nothing else:\n"
                "SCORE: <0-100>\n"
                "LABEL: <one word, caps>\n"
                "QUOTE: <one sentence describing the vibe, max 60 chars>\n\n"
                f"Chat excerpt:\n{transcript}"
            )
            raw = await self._agent.complete(prompt)
            score, label, quote = self._parse_vibe_response(raw)
            if score is None:
                logger.debug("vibe check: could not parse response: %s", raw[:200])
                return

            from kai.agent.tools.hardware import render_vibe_check

            result = render_vibe_check(score, label, quote)
            logger.info("epaper vibe check (%d%% %s): %s", score, label, result)
        except Exception:
            logger.debug("vibe check failed", exc_info=True)

    @staticmethod
    def _parse_vibe_response(raw: str) -> tuple[int | None, str, str]:
        """Parse 'SCORE: N\\nLABEL: word\\nQUOTE: text' into components."""
        score: int | None = None
        label = "UNKNOWN"
        quote = ""
        for line in raw.strip().splitlines():
            low = line.strip().lower()
            if low.startswith("score:"):
                try:
                    score = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif low.startswith("label:"):
                label = line.split(":", 1)[1].strip()
            elif low.startswith("quote:"):
                quote = line.split(":", 1)[1].strip()
        return score, label, quote

    async def status(self) -> None:
        waha = WahaSettings()
        client = WahaClient(waha)
        try:
            session = await client.get_session_status()
            if session:
                sname = session.get("name", "unknown")
                sstatus = session.get("status", "unknown")
                color = "green" if sstatus == "WORKING" else "yellow"
                console.print(f"  session  {sname} ([{color}]{sstatus}[/{color}])")
                me = session.get("me")
                if me:
                    console.print(f"  account  {me.get('pushName', 'unknown')}")
            else:
                console.print("  session  [dim]not found[/dim]")
            await self._show_profile(client)
        except Exception as exc:
            console.print(f"[red]{exc}[/red]")
            raise
        finally:
            await client.close()

    def _print_startup_config(self) -> None:
        waha_warnings = self._waha.validate_startup()
        core_warnings = self._settings.validate_startup()
        warnings = waha_warnings + core_warnings

        console.print(f"  waha     {self._waha.url}  [dim]session={self._waha.session}[/dim]")
        console.print(
            f"  llm      {self._settings.llm_model}  [dim]{self._settings.llm_api_base}[/dim]"
        )
        console.print(
            f"  webhook  {self._waha.webhook_host}:"
            f"{self._waha.webhook_port}{self._waha.webhook_path}"
        )
        if self._waha.webhook_public_host:
            console.print(f"  public   {self._waha.webhook_public_host}")
        console.print(f"  language {self._config.language}")
        tz = self._config.timezone or "(server local)"
        console.print(f"  timezone {tz}")
        if self._config.media.voice_enabled and self._stt:
            stt_type = type(self._stt).__name__
            console.print(f"  stt      {stt_type}  [dim]{self._stt_language}[/dim]")
        if self._waha.hmac_key:
            console.print("  hmac     [dim]configured[/dim]")

        from kai.agent.tools.hardware import epaper_available

        if epaper_available():
            console.print("  epaper   [green]detected[/green]  [dim]waveshare epd2in13_V3[/dim]")
        else:
            console.print("  epaper   [dim]not detected[/dim]")

        if not self._waha.hmac_key and self._waha.webhook_host not in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            console.print(
                "  [yellow]! WARNING: HMAC is not set and webhook is bound to "
                f"{self._waha.webhook_host}. The endpoint is unauthenticated.[/yellow]"
            )
            warnings.append(
                "Webhook is unauthenticated on non-loopback bind. "
                "Set KAI_WAHA_HMAC_KEY for production use."
            )

        if warnings:
            for w in warnings:
                console.print(f"  [yellow]! {w}[/yellow]")

    async def _show_profile(self, client: WahaClient) -> None:
        try:
            profile = await client.get_profile()
            if not profile:
                return
            name = profile.get("name", "")
            profile_id = profile.get("id", "")
            picture_url = profile.get("picture", "")

            picture_bytes = await client.get_profile_picture()
            if not picture_bytes and picture_url:
                picture_bytes = await client.download_image(picture_url)
            if picture_bytes:
                render_image_pixelated(picture_bytes, console, width=16)
                from kai.agent.tools.hardware import epaper_available, render_image_to_epaper

                if epaper_available():
                    result = render_image_to_epaper(picture_bytes, title=name or "kai")
                    logger.info("boot epaper render: %s", result)

            if name:
                console.print(f"  [cyan]{name}[/cyan]")
            if profile_id:
                console.print(f"  [dim]{profile_id}[/dim]")
        except Exception as exc:
            logger.debug("Failed to load profile: %s", exc)

    async def _connect_waha(self) -> None:
        client = WahaClient(self._waha)
        try:
            status = await client.get_session_status()
            if not status:
                console.print(f"[red]session '{self._waha.session}' not found[/red]")
                console.print("[dim]create and start it from the WAHA dashboard first[/dim]")
                raise SystemExit(1)

            session_status = status.get("status", "unknown")
            color = "green" if session_status == "WORKING" else "yellow"
            console.print(f"  session  {self._waha.session}  ([{color}]{session_status}[/{color}])")
            if session_status != "WORKING":
                console.print(f"  [yellow]expected WORKING, got {session_status}[/yellow]")

            me = status.get("me")
            if me and me.get("_serialized"):
                self._bot_ids.add(me["_serialized"])
                logger.info("Bot identity from session: %s", me["_serialized"])

            profile = await client.get_profile()
            if profile and profile.get("id"):
                self._bot_ids.add(profile["id"])
                logger.info("Bot identity from profile: %s", profile["id"])

            if self._bot_ids:
                console.print(f"  bot ids  {', '.join(sorted(self._bot_ids))}")

            await self._show_profile(client)

            public_host = self._waha.webhook_public_host or f"localhost:{self._waha.webhook_port}"
            webhook_url = _build_webhook_url(public_host, self._waha.webhook_path)

            await client.update_session_webhook(webhook_url)
            console.print(f"  webhook  {webhook_url}")
        except SystemExit:
            raise
        except Exception as exc:
            console.print(f"[red]failed to connect to WAHA: {exc}[/red]")
            logger.error("Startup failed: %s", exc)
            raise SystemExit(1) from exc
        finally:
            await client.close()

    def _seen_store_path(self, settings: Settings) -> Path | None:
        """Resolve the seen-IDs file path, anchored to the bot dir.

        Mirrors :meth:`BaseBot.setup_task_scheduler`'s path logic so the
        file lives alongside ``<name>.tasks.json`` (CWD-independent). Returns
        None when persistence is disabled (``tasks_folder`` unset), in
        which case :class:`SeenStore` stays in-memory only.
        """
        if settings.tasks_folder is None:
            return None
        folder = Path(settings.tasks_folder)
        if not folder.is_absolute():
            folder = self.bot_dir / folder
        path = folder / f"{self.name}.seen.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _sleep_store_path(self, settings: Settings) -> Path | None:
        """Resolve the sleep-state file path, anchored to the bot dir.

        Same convention as :meth:`_seen_store_path`; writes to
        ``<name>.sleep.json`` alongside the other per-bot state files.
        """
        if settings.tasks_folder is None:
            return None
        folder = Path(settings.tasks_folder)
        if not folder.is_absolute():
            folder = self.bot_dir / folder
        path = folder / f"{self.name}.sleep.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    async def _is_seen_message(self, message_id: str) -> bool:
        if not message_id or self._seen_store is None:
            return False
        if self._seen_store.is_seen(message_id):
            return True
        # New message: record it, offloading the disk write off the event loop.
        await self._seen_store.add_async(message_id)
        return False

    def _get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            _bounded_dict_set(self._chat_locks, chat_id, lock, _CHAT_LOCKS_MAX)
        else:
            _bounded_dict_set(self._chat_locks, chat_id, lock, _CHAT_LOCKS_MAX)
        return lock

    async def _handle_message(self, payload: dict) -> None:
        msg = payload.get("payload", {})
        message_id = msg.get("id") or msg.get("_data", {}).get("id") or ""
        if await self._is_seen_message(str(message_id)):
            logger.debug("Ignoring duplicate message: %s", message_id)
            return

        if msg.get("fromMe", False):
            logger.debug("Ignoring own message")
            return

        meta = parse_message(payload, bot_ids=self._bot_ids)

        logger.debug(
            "Raw message fields: from=%s author=%s to=%s",
            msg.get("from"),
            msg.get("author"),
            msg.get("to"),
        )

        if not should_process_chat_message(
            meta.chat_id,
            meta.sender_id,
            set(self._config.whitelist),
            set(self._config.blacklist),
        ):
            return

        text = (msg.get("body") or "").strip()
        media = extract_media(msg) if msg.get("type") not in ("chat", "text") else None

        # WAHA webhooks deliver messages without downloaded media (media is
        # null, mediaUrl is absent). When the message has media but we couldn't
        # extract it, re-fetch from the REST API with downloadMedia=true so
        # extract_media picks up media.url.
        if media is None and msg.get("hasMedia") and msg.get("type") not in ("chat", "text"):
            refetched = await self._fetch_message_media(meta.chat_id, str(message_id))
            if refetched is not None:
                msg = refetched
                media = extract_media(msg)
                if not text:
                    text = (msg.get("body") or "").strip()

        if not text and media is None:
            logger.debug("Skipping empty message with no media")
            return

        async with self._get_chat_lock(meta.chat_id):
            roster = self._rosters.setdefault(meta.chat_id, {})
            roster[meta.sender_id] = meta.sender_name
            if meta.is_group:
                await self._refresh_group_roster(meta.chat_id, roster)
            _bounded_dict_set(self._rosters, meta.chat_id, roster, _ROSTER_MAX_CHATS)

            logger.info("Message from %s in %s: %.100s", meta.sender_name, meta.chat_id, text)
            console.print(f"[dim]< {meta.sender_name}[/dim]  {text}")

            self._vibe_msg_count += 1
            if self._vibe_msg_count >= self._vibe_interval:
                self._vibe_msg_count = 0
                asyncio.create_task(self._run_vibe_check(meta.chat_id))

            enriched_text = self._enrich_message_text(msg, text)

            # Voice/audio notes are transcribed on EVERY message — not just turns
            # where Kai replies — so the spoken content is captured into
            # conversation history regardless of whether the bot responds. A
            # background voice note with an empty body would otherwise be
            # observed as nothing, losing what was said. Only run when STT
            # (whisper) is available; otherwise the raw caption/body is kept.
            if (
                media
                and media.type in (MediaType.VOICE, MediaType.AUDIO)
                and self._config.media.voice_enabled
                and self._stt
            ):
                media_bytes = await self._resolve_media_bytes(media)
                if media_bytes:
                    transcription = await self._stt.transcribe(
                        media_bytes, mime_type=media.mime_type
                    )
                    if transcription:
                        voice_tag = f"[voice note: {transcription}]"
                        enriched_text = (
                            f"{voice_tag}\n{enriched_text}" if enriched_text else voice_tag
                        )
                    else:
                        logger.warning("Voice transcription returned empty")
                else:
                    logger.warning("Failed to resolve voice media")

            replies_to_bot = self._is_reply_to_bot(msg)
            context = MessageContext(
                sender_name=meta.sender_name,
                sender_id=meta.sender_id,
                chat_id=meta.chat_id,
                is_group=meta.is_group,
                mentions_bot=meta.mentions_bot,
                replies_to_bot=replies_to_bot,
            )

            self.set_task_context(
                chat_id=meta.chat_id, owner_id=meta.sender_id, tz_hint=self._config.timezone
            )

            is_sleeping = (
                self._sleep_store.is_sleeping(meta.chat_id)
                if self._sleep_store is not None
                else False
            )

            # --- Sleep mode ---
            if is_sleeping:
                await self._handle_sleep_mode(meta, context, enriched_text, replies_to_bot, roster)
                return

            summoned = self._should_respond(
                text,
                meta.is_group,
                meta.mentions_bot,
                has_media=media is not None,
                replies_to_bot=replies_to_bot,
            )
            organic = not summoned and self._should_organically_participate(
                meta.chat_id, text, is_group=meta.is_group
            )

            if not summoned and not organic:
                if enriched_text.strip():
                    await self._agent.observe(
                        enriched_text, conversation_id=meta.chat_id, context=context
                    )
                self._mark_skipped(meta.chat_id)
                logger.debug("Not responding to group message: %.80s", text)
                return

            images: list[bytes] = []
            if media and media.type == MediaType.IMAGE and self._config.media.image_enabled:
                media_bytes = await self._resolve_media_bytes(media)
                if media_bytes:
                    images.append(media_bytes)
                    image_tag = "[image attached]"
                    enriched_text = f"{image_tag}\n{enriched_text}" if enriched_text else image_tag
                else:
                    logger.warning("Failed to resolve image media, skipping image")

            # Voice/audio transcription already happened above (before the
            # summon decision) so background notes are captured into history;
            # nothing to do here on the reply path.

            ig = await self._enrich_instagram(text)
            if ig is not None:
                ig_text, ig_images = ig
                images.extend(ig_images)
                if ig_text:
                    tag = f"[instagram post:\n  {ig_text}]"
                    enriched_text = f"{tag}\n{enriched_text}" if enriched_text else tag

            per_chat_prompt = self._build_per_chat_prompt(meta.chat_id, meta.is_group, roster)
            extra_context_parts: list[str] = []
            if per_chat_prompt:
                extra_context_parts.append(per_chat_prompt)
            if organic and not summoned:
                extra_context_parts.append(
                    "Nobody addressed you directly. Only reply if you have something "
                    "genuinely worth adding; otherwise use <<silent>>."
                )
            elif summoned and not (meta.mentions_bot or replies_to_bot) and meta.is_group:
                # Summoned by keyword/name-drop rather than a direct @-mention or
                # reply. A name-drop is only a direct address if clearly aimed at
                # the bot; otherwise the model should prefer staying quiet.
                extra_context_parts.append(
                    "You were summoned because your name was mentioned in the "
                    "message, not via a direct @-mention or a reply to you. "
                    "Re-read the message: if it is not clearly aimed at you, "
                    "reply <<silent>>."
                )
            extra_context = "\n\n".join(extra_context_parts) or None

            # DMs and hard direct addresses (mention/reply) must never ghost the
            # user. A group name-drop is a soft summon — the model may decline.
            hard_addressed = (not meta.is_group) or meta.mentions_bot or replies_to_bot
            allow_silence = meta.is_group and not hard_addressed

            reply = await self._agent.chat(
                enriched_text,
                conversation_id=meta.chat_id,
                context=context,
                extra_system_context=extra_context,
                images=images or None,
                reply_style=REPLY_STYLE,
                allow_silence=allow_silence,
            )
            reply = strip_reasoning_channels(reply)

            # A leaked/unparsed tool call must never reach the chat. On a
            # hard-direct-address turn (DM / @-mention / reply-to-bot) the user
            # is waiting and must not be ghosted: retry once with a nudge to
            # emit plain text. On a background turn, treat it as silence.
            if has_tool_call_leak(reply):
                if hard_addressed:
                    logger.warning(
                        "Leaked tool-call reply on direct address for %s; retrying",
                        meta.chat_id,
                    )
                    retry = await self._agent.chat(
                        "",
                        conversation_id=meta.chat_id,
                        context=context,
                        extra_system_context=(
                            "Your previous reply contained raw tool-call "
                            "markup that can't be sent. Reply now with ONLY a "
                            "short plain-text WhatsApp message answering the "
                            "last message — no tool calls, no markup, no "
                            "tags, no acknowledgement of this instruction."
                        ),
                        reply_style=REPLY_STYLE,
                        allow_silence=False,
                    )
                    retry = strip_reasoning_channels(retry)
                    if retry and not has_tool_call_leak(retry) and not is_silent_reply(retry):
                        reply = retry
                    else:
                        logger.warning(
                            "Retry still leaked/empty on direct address %s",
                            meta.chat_id,
                        )
                        console.print("[dim]> (no clean reply after retry)[/dim]")
                        await self._abort_turn(meta, enriched_text, images, context)
                        return
                else:
                    logger.warning("Discarding leaked tool-call reply for %s", meta.chat_id)
                    console.print("[dim]> (dropped tool-call leak)[/dim]")
                    await self._abort_turn(meta, enriched_text, images, context)
                    return

            if is_silent_reply(reply):
                # Only an anchored whole-turn <<silent>> drops the reply. A
                # stray token mixed into real content is stripped by
                # _post_process so the surrounding text still ships.
                logger.info("Bot chose silence for %s", meta.chat_id)
                console.print("[dim]> (silent)[/dim]")
                await self._abort_turn(meta, enriched_text, images, context)
                return

            # The model may decide to go to sleep (in any language/dialect) by
            # emitting <<sleep>>. The accompanying text becomes the goodbye; if
            # none, a default acknowledgment is sent.
            if has_sleep_token(reply):
                ack = strip_sleep_token(reply) or DEFAULT_SLEEP_ACK
                ack = self._post_process(ack)
                self._sleep_store.set(meta.chat_id, True)
                console.print(f"[green]>[/green]  {ack}  [dim](going to sleep)[/dim]")
                if self._config.mentions_enabled:
                    resolved = resolve_mentions(
                        ack, roster, bot_ids=self._bot_ids, is_group=meta.is_group
                    )
                    ack, mentions = resolved.text, resolved.mentions
                else:
                    ack, mentions = strip_mention_markup(ack), []
                try:
                    await self._send_with_retry(meta.chat_id, ack, mentions or None)
                except Exception as exc:
                    logger.error("Failed to send sleep ack to %s: %s", meta.chat_id, exc)
                self._epaper_sleep_screen()
                return

            reply = self._post_process(reply)
            console.print(f"[green]>[/green]  {reply}")

            await self._send_reply(meta, roster, reply)
            self._mark_replied(meta.chat_id)

    async def _handle_sleep_mode(
        self, meta, context, enriched_text: str, replies_to_bot: bool, roster: dict
    ) -> None:
        """Handle an inbound message while the bot is sleeping in this chat.

        While asleep, the bot never speaks on its own. It only gets a chance
        to wake when someone directly addresses it (mention or reply), and
        even then the model decides — if it returns <<silent>> it stays
        asleep, any real reply clears the sleep state.
        """
        addressed = meta.mentions_bot or replies_to_bot or not meta.is_group
        if enriched_text.strip():
            await self._agent.observe(enriched_text, conversation_id=meta.chat_id, context=context)
        if not addressed:
            logger.debug("Bot is sleeping in %s; observing only", meta.chat_id)
            return
        # Directly addressed while asleep: let the model decide whether
        # to wake up. A real reply wakes it; <<silent>> keeps it asleep.
        reply = await self._agent.chat(
            enriched_text,
            conversation_id=meta.chat_id,
            context=context,
            extra_system_context=(
                "You are currently asleep in this chat. If this message is "
                "genuinely trying to wake you, reply normally and you will "
                "wake up. If it isn't (e.g. you were merely mentioned in "
                "passing), reply <<silent>> and stay asleep."
            ),
            images=None,
            reply_style=REPLY_STYLE,
            allow_silence=True,
        )
        reply = strip_reasoning_channels(reply)
        if is_silent_reply(reply) or has_sleep_token(reply) or has_tool_call_leak(reply):
            logger.info("Bot stays asleep for %s", meta.chat_id)
            console.print("[dim]> (still sleeping)[/dim]")
            return
        # Waking up: clear sleep and deliver the reply.
        self._sleep_store.set(meta.chat_id, False)
        console.print("[dim]> (woke up)[/dim]")
        reply = self._post_process(reply)
        console.print(f"[green]>[/green]  {reply}")
        await self._send_reply(meta, roster, reply)
        self._mark_replied(meta.chat_id)
        self._epaper_wake_screen()

    async def send_text(self, chat_id: str, text: str) -> None:
        """Send a text message into ``chat_id`` via WAHA (replies, task output, ...)."""
        console.print(f"[green]>[/green]  {text}")
        await self._send_with_retry(chat_id, text)

    async def _send_with_retry(
        self,
        chat_id: str,
        text: str,
        mentions: list[str] | None = None,
    ) -> None:
        client = self._waha_client
        if client is None:
            client = WahaClient(self._waha)
            should_close = True
        else:
            should_close = False
        try:
            last_exc: Exception | None = None
            for attempt in range(_SEND_MAX_RETRIES):
                try:
                    await client.send_message(chat_id, text, mentions=mentions)
                    return
                except Exception as exc:
                    last_exc = exc
                    status_code = getattr(exc, "response", None)
                    status_code = getattr(status_code, "status_code", None) if status_code else None
                    if status_code and 400 <= status_code < 500:
                        raise
                    if attempt < _SEND_MAX_RETRIES - 1:
                        delay = _SEND_RETRY_BASE_DELAY * (2**attempt)
                        logger.warning(
                            "Send attempt %d/%d failed, retrying in %.1fs: %s",
                            attempt + 1,
                            _SEND_MAX_RETRIES,
                            delay,
                            exc,
                        )
                        await asyncio.sleep(delay)
            raise last_exc  # type: ignore[misc]
        finally:
            if should_close:
                await client.close()

    async def _send_reply(self, meta, roster: dict[str, str], reply: str) -> None:
        """Resolve mentions and send a reply, logging send failures."""
        if self._config.mentions_enabled:
            resolved = resolve_mentions(
                reply, roster, bot_ids=self._bot_ids, is_group=meta.is_group
            )
            out_text, mentions = resolved.text, resolved.mentions
        else:
            out_text, mentions = strip_mention_markup(reply), []
        if mentions:
            logger.info("Mentions applied: %d", len(mentions))
        try:
            await self._send_with_retry(meta.chat_id, out_text, mentions or None)
        except Exception as exc:
            logger.error("Failed to send reply to %s: %s", meta.chat_id, exc)
            console.print(f"[red]send failed: {meta.chat_id}: {exc}[/red]")

    async def _resolve_media_bytes(self, media: MediaAttachment) -> bytes | None:
        if media.data is not None:
            return media.data
        if media.url:
            client = self._waha_client or WahaClient(self._waha)
            should_close = self._waha_client is None
            try:
                return await client.download_media(media.url, self._config.media.max_size_mb)
            finally:
                if should_close:
                    await client.close()
        return None

    async def _fetch_message_media(self, chat_id: str, message_id: str) -> dict | None:
        """Re-fetch a message from WAHA with downloadMedia=true."""
        client = self._waha_client or WahaClient(self._waha)
        should_close = self._waha_client is None
        try:
            return await client.get_message(chat_id, message_id, download_media=True)
        except Exception as exc:
            logger.warning("Failed to fetch media for message %s: %s", message_id, exc)
            return None
        finally:
            if should_close:
                await client.close()

    async def _enrich_instagram(self, text: str) -> tuple[str | None, list[bytes]] | None:
        """If text contains an IG post URL, return (post_data, image_bytes_list).

        Mirrors the voice-note enrichment: a tagged text block for the agent
        plus image bytes for the vision channel. Returns None when there's
        nothing to enrich or curl_cffi is unavailable, so the message still
        reaches the agent untouched.
        """
        if not getattr(self._config.media, "instagram_enabled", True):
            return None
        shortcode = extract_instagram_shortcode(text)
        if shortcode is None:
            return None
        try:
            import curl_cffi  # noqa: F401 — probe availability
        except ImportError:
            logger.warning("curl_cffi not installed; skipping IG enrichment")
            return None
        try:
            # curl_cffi is synchronous and network-bound; keep the event loop
            # free so concurrent chats aren't blocked.
            data_text, image_bytes_list = await asyncio.to_thread(fetch_instagram_post, shortcode)
        except Exception as exc:
            logger.warning("IG fetch failed for shortcode %s: %s", shortcode, exc)
            return None
        if not data_text and not image_bytes_list:
            return None
        return data_text, image_bytes_list

    def _learn_bot_identity(self, phone_jid: str | None, lid_jid: object) -> None:
        """Adopt a group's @lid identity for the bot when it can be matched.

        ``phone_jid`` is a participant's @c.us phone JID; ``lid_jid`` is the
        paired @lid. If the phone JID shares its digit prefix with a known bot
        identity, both JIDs are added to ``self._bot_ids`` so subsequent
        mention/reply detection (which matches against ``_bot_ids``) recognizes
        the bot when it is addressed by @lid.
        """
        if not self._bot_ids or not phone_jid:
            return
        phone_digits = phone_jid.split("@")[0]
        if not any(bid.split("@")[0] == phone_digits for bid in self._bot_ids):
            return
        if phone_jid not in self._bot_ids:
            self._bot_ids.add(phone_jid)
        if isinstance(lid_jid, str) and lid_jid and lid_jid not in self._bot_ids:
            self._bot_ids.add(lid_jid)
            logger.info("Learned bot @lid identity from group roster: %s", lid_jid)

    def _is_reply_to_bot(self, msg: dict) -> bool:
        reply_to = msg.get("replyTo")
        if not reply_to or not isinstance(reply_to, dict):
            return False
        participant = reply_to.get("participant")
        if isinstance(participant, dict):
            sender = participant.get("_serialized", "")
        else:
            sender = str(participant) if participant else ""
        if not sender:
            return False
        sender_digits = sender.split("@")[0]
        return any(jid.split("@")[0] == sender_digits for jid in self._bot_ids)

    def _should_respond(
        self,
        text: str,
        is_group: bool,
        mentions_bot: bool,
        has_media: bool = False,
        replies_to_bot: bool = False,
    ) -> bool:
        """Return True when a message forces (summons) the bot to speak."""
        if not is_group:
            return True
        if mentions_bot:
            return True
        if replies_to_bot:
            return True
        keyword = self._config.trigger_keyword.strip().lower()
        if not keyword:
            return True
        return re.search(rf"\b{re.escape(keyword)}\b", text.lower()) is not None

    def _should_organically_participate(
        self,
        chat_id: str,
        text: str,
        *,
        is_group: bool,
    ) -> bool:
        """Delegate to the extracted participation logic."""
        return should_organically_participate(
            chat_id,
            text,
            is_group=is_group,
            participation_cfg=self._config.participation,
            last_reply_at=self._last_reply_at,
            consecutive_replies=self._consecutive_replies,
        )

    def _post_process(self, reply: str) -> str:
        return post_process(reply)

    def _mark_replied(self, chat_id: str) -> None:
        self._last_reply_at[chat_id] = time.monotonic()
        self._consecutive_replies[chat_id] = self._consecutive_replies.get(chat_id, 0) + 1

    def _mark_skipped(self, chat_id: str) -> None:
        self._consecutive_replies[chat_id] = 0

    async def _abort_turn(
        self,
        meta: MessageMetadata,
        enriched_text: str,
        images: list[bytes],
        context: MessageContext | None,
    ) -> None:
        """Drop the current reply: mark skipped, observe the inbound message so
        the conversation history still reflects it, then end the turn.

        Shared by the tool-call-leak and silence drop paths so their behavior
        can't drift apart.
        """
        self._mark_skipped(meta.chat_id)
        if enriched_text.strip():
            await self._agent.observe(
                enriched_text,
                conversation_id=meta.chat_id,
                context=context,
                images=images or None,
            )

    def _render_tool_call(self, tool_name: str, tool_kwargs: dict, result: str) -> None:
        """Print tool usage to the console, mirroring message rendering."""
        args = ""
        if tool_kwargs:
            primary = next(iter(tool_kwargs.values()))
            if isinstance(primary, str) and len(primary) > 60:
                primary = primary[:57] + "..."
            args = f"({primary})" if primary is not None else "()"
        preview = result.replace("\n", " ").strip()
        if len(preview) > 80:
            preview = preview[:77] + "..."
        suffix = f"[dim] \u2192 {preview}[/dim]" if preview else ""
        console.print(f"[dim]> \U0001f527 {tool_name}{args}[/dim]" + suffix, highlight=False)

    def _build_per_chat_prompt(
        self, chat_id: str, is_group: bool, roster: dict[str, str]
    ) -> str | None:
        if not is_group or not roster:
            return None
        names = list(roster.values())
        if len(names) > 30:
            roster_line = f"People in this chat: {', '.join(names[:30])} (+{len(names) - 30} more)"
        else:
            roster_line = f"People in this chat: {', '.join(names)}"
        parts = [roster_line]
        admin_names = [
            roster[jid] for jid in self._group_admins.get(chat_id, set()) if jid in roster
        ]
        if admin_names:
            parts.append(f"Admins: {', '.join(admin_names[:10])}")
        return "\n".join(parts)

    async def _refresh_group_roster(self, chat_id: str, roster: dict[str, str]) -> None:
        """Refresh a group's roster from WAHA's participant list.

        Called on every inbound group message but rate-limited per chat by a
        TTL so a fast chat doesn't hammer the API.
        """
        now = time.monotonic()
        if now - self._roster_refreshed_at.get(chat_id, 0.0) < _ROSTER_REFRESH_TTL:
            return

        client = self._waha_client or WahaClient(self._waha)
        should_close = self._waha_client is None
        try:
            participants = await client.get_chat_participants(chat_id)
        except Exception as exc:
            logger.warning("Failed to fetch participants for %s: %s", chat_id, exc)
            return
        finally:
            if should_close:
                await client.close()

        names_by_digits: dict[str, str] = {}
        for jid, name in roster.items():
            if name:
                names_by_digits[jid.split("@")[0]] = name

        admins: set[str] = set()
        left_jids: set[str] = set()
        for entry in participants:
            if not isinstance(entry, dict):
                continue
            pn = entry.get("pn") or entry.get("id") or ""
            if not isinstance(pn, str) or not pn:
                continue
            # Learn the bot's own @lid identity for this group. WAHA addresses
            # the bot by an opaque @lid in many groups, but the bot only knows
            # its @c.us phone JID from the profile. The participants entry pairs
            # both (`pn` = phone JID, `id` = @lid), so when an entry's phone JID
            # matches a known bot id we adopt its @lid too — without this,
            # mention and reply-to-bot detection silently fail in @lid groups.
            self._learn_bot_identity(pn, entry.get("id"))
            role = entry.get("role", "participant")
            if role == "left":
                left_jids.add(pn)
                continue
            if role in ("admin", "superadmin"):
                admins.add(pn)
            digits = pn.split("@")[0]
            name = names_by_digits.get(digits)
            if name:
                for existing_jid in [j for j in roster if j.split("@")[0] == digits]:
                    if existing_jid != pn:
                        roster.pop(existing_jid, None)
                roster[pn] = name

        for jid in left_jids:
            roster.pop(jid, None)

        self._group_admins[chat_id] = admins
        self._roster_refreshed_at[chat_id] = time.monotonic()

    def _enrich_message_text(self, raw_msg: dict, text: str) -> str:
        chat_id = raw_msg.get("from", "")
        roster = self._rosters.get(chat_id, {})
        is_group = GROUP_SUFFIX in chat_id
        text = resolve_inbound_mentions(text, roster, is_group=is_group)

        parts: list[str] = []

        reply_to = raw_msg.get("replyTo")
        if reply_to and isinstance(reply_to, dict):
            reply_body = (reply_to.get("body") or "").strip()
            # Skip media attachments: WAHA delivers a replied-to image/audio as a
            # base64 blob (JPEG/PNG/WebP/…) or marks hasMedia. Neither carries
            # useful text, and a base64 blob would bloat and corrupt the turn.
            has_media = bool(reply_to.get("hasMedia")) or looks_like_base64_media(reply_body)
            if reply_body and not has_media:
                # Resolve inbound @<digits> mentions in the quoted text so the
                # model sees names, not raw LID/phone digits.
                reply_body = resolve_inbound_mentions(reply_body, roster, is_group=is_group)
                # Cap length so a long forwarded quote doesn't flood the turn.
                if len(reply_body) > 300:
                    reply_body = reply_body[:300].rstrip() + "…"
                participant = reply_to.get("participant")
                if isinstance(participant, dict):
                    reply_sender = participant.get("_serialized", "")
                else:
                    reply_sender = str(participant) if participant else ""
                roster = self._rosters.get(raw_msg.get("from", ""), {})
                reply_name = _sanitize_name(
                    roster.get(reply_sender)
                    or (reply_sender.split("@")[0] if reply_sender else "someone")
                )
                parts.append(f"[replying to {reply_name}: {reply_body}]")

        # Extract shared URLs, stripping trailing punctuation that \S+ greedily
        # captures (e.g. the ")" in "(https://x.com)"). Instagram URLs are
        # fetched and tagged separately by _enrich_instagram, so they're excluded
        # here to avoid duplicating the link in two tags.
        raw_urls = re.findall(r"https?://\S+", text) or []
        urls: list[str] = []
        for u in raw_urls:
            # Strip trailing punctuation that \S+ greedily captures (e.g. the
            # ")" in "(https://x.com)"), but only while it's genuinely
            # unbalanced so a real URL like
            # https://en.wikipedia.org/wiki/Foo_(bar) keeps its closing paren.
            u = _strip_unbalanced_trailing_punct(u)
            if u and "instagram.com" not in u and u not in urls:
                urls.append(u)
        if urls:
            parts.append(f"[links in message: {', '.join(urls)}]")

        if parts:
            return "\n".join(parts) + "\n" + text
        return text
