import asyncio
import base64
import json
import logging
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from llama_index.core.tools import FunctionTool
from rich.console import Console

from kai.agent.context import MessageContext
from kai.agent.core import ChatResult, KaiAgent
from kai.agent.tools import WEB_WORKFLOW_INSTRUCTIONS
from kai.agent.tools.conversation import register_conversation_tools
from kai.agent.tools.email import DEFAULT_DISPLAY_NAME
from kai.bots.base import BaseBot, TellResult
from kai.bots.waha.actions import action_cls_for_turn
from kai.bots.waha.client import WahaClient
from kai.bots.waha.config import WahaSettings, get_waha_settings
from kai.bots.waha.history import register_chat_history_tool
from kai.bots.waha.instagram import extract_instagram_shortcode, fetch_instagram_post
from kai.bots.waha.jid import sanitize_display_name
from kai.bots.waha.media import MediaAttachment, MediaType, extract_media
from kai.bots.waha.mentions import resolve_inbound_mentions, resolve_mentions, strip_mention_markup
from kai.bots.waha.payload import GROUP_SUFFIX, MessageMetadata, parse_message
from kai.bots.waha.processing import (
    REPLY_STYLE,
    looks_like_base64_media,
    post_process,
    should_organically_participate,
    should_send_voice_followup,
)
from kai.bots.waha.seen_store import SeenStore
from kai.bots.waha.setup import (
    _DEFAULT_PARTICIPATION_COOLDOWN,
    _DEFAULT_PARTICIPATION_RATE,
    _DEFAULT_PARTICIPATION_STREAK_MAX,
    _DEFAULT_VOICE_NOTE_COOLDOWN,
    _DEFAULT_VOICE_NOTE_RATE,
    BotConfig,
    MediaConfig,
    ParticipationConfig,
)
from kai.bots.waha.sleep_store import SleepStore
from kai.bots.waha.stt import NoopSTT, STTProvider, create_stt_provider, resolve_whisper_language
from kai.bots.waha.tts import (
    SUPPORTED_KOKORO_LANGUAGE_NAMES,
    check_kokoro_available,
    detect_kokoro_lang,
    parse_voice_map,
    resolve_kokoro_lang,
    resolve_kokoro_voice,
    synthesize,
)
from kai.bots.waha.video import compress_video, resolve_ffmpeg
from kai.bots.waha.webhook import create_webhook_app
from kai.bots.waha.youtube import extract_youtube_video_id, fetch_youtube_transcript
from kai.cli import BotStartupError
from kai.config.filters import should_process_chat_message
from kai.config.prompts import load_system_prompt
from kai.config.settings import Settings
from kai.utils.terminal import render_image_pixelated

logger = logging.getLogger(__name__)
console = Console()

_SEEN_IDS_MAX = 2048
_ROSTER_MAX_CHATS = 1024
_CHAT_LOCKS_MAX = 1024
_VOICE_LANG_MAX = 1024
# Group participant lists are refreshed on inbound group messages but cached
# per chat so we don't hammer WAHA on every message in a fast chat.
_ROSTER_REFRESH_TTL = 300.0

# Default goodbye sent when the model chooses the ``sleep`` action with no
# accompanying text (replaces the old ``DEFAULT_SLEEP_ACK`` string token).
_SLEEP_ACK = "going quiet, ping me if you need me"

# Re-export public API so ``from kai.bots.waha import ...`` keeps working.
__all__ = [
    "Bot",
    "BotConfig",
    "MediaConfig",
    "ParticipationConfig",
    "_build_webhook_url",
]


def _build_webhook_url(public_host: str, webhook_path: str, webhook_port: int | None = None) -> str:
    """Build the URL WAHA should POST webhook events to.

    ``public_host`` may already carry its own scheme/port (e.g. a full
    ``https://example.com`` public URL) — in that case it is used verbatim
    and ``webhook_port`` is ignored, since the caller has already resolved
    the externally reachable address (reverse proxy, etc). Otherwise
    ``webhook_port`` is required to reach the bot's listener: dropping it
    silently sends webhooks to the host's default port (80), which has no
    listener and makes the bot appear to receive no messages at all.
    """
    parsed = urlparse(public_host)
    if parsed.scheme:
        return f"{parsed.scheme}://{parsed.netloc}{webhook_path}"
    if webhook_port is not None and ":" not in public_host:
        return f"http://{public_host}:{webhook_port}{webhook_path}"
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


def _extract_video_thumbnail(msg: dict) -> bytes | None:
    """Decode the WAHA ``_data.body`` JPEG thumbnail for a video message."""
    body = ((msg.get("_data") or {}).get("body")) or ""
    if body and body.startswith(("/9j/", "iVBOR", "UklGR")):
        try:
            return base64.b64decode(body)
        except (ValueError, OSError) as exc:
            logger.warning("Failed to decode video thumbnail: %s", exc)
            return None
    return None


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
        self._last_voice_at: dict[str, float] = {}
        self._last_voice_lang: OrderedDict[str, str] = OrderedDict()
        self._server: uvicorn.Server | None = None
        self._shutting_down = asyncio.Event()
        self._stt: STTProvider | None = None
        self._stt_language: str = "auto"
        self._tts_available: bool = False
        self._tts_voice: str = ""
        self._tts_lang: str | None = "en-us"
        self._waha_client: WahaClient | None = None
        self._seen_store: SeenStore | None = None
        self._ffmpeg_path: str | None = None

    def configure(self, agent: KaiAgent, settings: Settings, *, voice: str | None = None) -> None:
        self._agent = agent
        self._settings = settings
        waha = get_waha_settings()
        # --voice CLI flag overrides KAI_WAHA_KOKORO_VOICE for this run.
        if voice:
            waha = waha.model_copy(update={"kokoro_voice": voice})
        self._waha = waha
        self._config = self._load_config()
        if settings.agent_language_explicit:
            self._config = self._config.model_copy(update={"language": settings.agent_language})
        self._prompt = self._load_prompt()
        agent.set_system_prompt(self._prompt)
        agent.set_temperature(self._config.temperature)
        agent.set_tool_workflow(WEB_WORKFLOW_INSTRUCTIONS)
        agent.set_tool_call_callback(self._render_tool_call)
        agent.set_timezone(self._config.timezone)
        self.setup_task_scheduler(agent, settings)
        register_chat_history_tool(agent, bot=self)
        register_conversation_tools(agent, tool_context=self._tool_context)
        self._seen_store = SeenStore(self._seen_store_path(settings), max_size=_SEEN_IDS_MAX)
        self._sleep_store = SleepStore(self._sleep_store_path(settings))
        if self._config.media.stt_enabled:
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
            )
            self._stt_language = whisper_lang

        self._ffmpeg_path = (
            resolve_ffmpeg(self._waha.ffmpeg_path) if self._config.media.video_enabled else None
        )

        if self._waha.kokoro_enabled and self._config.media.tts_enabled:
            self._tts_voice = self._waha.kokoro_voice
            # Resolve the Kokoro lang code: an explicit KAI_WAHA_KOKORO_LANG
            # wins; otherwise derive it from the bot's configured language
            # (so an English bot gets lang="en-us" without extra configuration).
            # ``None`` means the configured language has no Kokoro v1.0 voice
            # at all (e.g. German, Dutch, Polish) — don't coerce it to
            # "en-us", or replies get English phonemization in whatever
            # unrelated voice was picked for that language.
            if self._waha.kokoro_lang:
                self._tts_lang = self._waha.kokoro_lang
            else:
                self._tts_lang = resolve_kokoro_lang(self._config.language)
            # Per-language voice overrides. Seeded with the operator's
            # configured primary voice so a detected language that matches the
            # configured one keeps the operator's chosen voice; explicit
            # KAI_WAHA_KOKORO_VOICE_MAP entries override/extend that. Voice
            # replies detect the reply language at synthesis time, so a bot
            # can mix languages in the same conversation. Skipped entirely
            # when the configured language isn't Kokoro-supported — there is
            # no correct lang code to seed the operator's voice under.
            self._voice_map: dict[str, str] = (
                {self._tts_lang: self._tts_voice} if self._tts_lang else {}
            )
            self._voice_map.update(parse_voice_map(self._waha.kokoro_voice_map))
            self._tts_available, reason = check_kokoro_available(
                host=self._waha.kokoro_server_host,
                port=self._waha.kokoro_server_port,
            )
            if not self._tts_available:
                logger.warning("Kokoro TTS unavailable: %s", reason)
        else:
            self._tts_available = False
            self._voice_map = {}

    def _load_config(self, config_path: Path | None = None) -> BotConfig:
        path = config_path or self.resolve_config_path()
        if path is None or not path.exists():
            logger.info("No config override for bot '%s'; using BotConfig() defaults", self.name)
            return BotConfig()
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

        media_data = data.get("media", {})
        participation_data = data.get("participation", {})

        return BotConfig(
            trigger_keyword=str(data.get("trigger_keyword", "kai")).strip(),
            whitelist=_parse_id_list(data.get("whitelist"), "whitelist"),
            blacklist=_parse_id_list(data.get("blacklist"), "blacklist"),
            language=str(data.get("language", "English")),
            timezone=str(data.get("timezone", "")).strip() or None,
            mentions_enabled=bool(data.get("mentions_enabled", True)),
            temperature=float(data.get("temperature", 0.4)),
            display_name=str(data.get("display_name", "")).strip() or DEFAULT_DISPLAY_NAME,
            media=MediaConfig(
                image_enabled=bool(media_data.get("image_enabled", True)),
                stt_enabled=bool(
                    media_data.get("stt_enabled", media_data.get("voice_enabled", True))
                ),
                tts_enabled=bool(media_data.get("tts_enabled", True)),
                video_enabled=bool(media_data.get("video_enabled", True)),
                instagram_enabled=bool(media_data.get("instagram_enabled", True)),
                max_size_mb=media_data.get("max_size_mb", 10),
            ),
            participation=ParticipationConfig(
                enabled=bool(participation_data.get("enabled", True)),
                rate=participation_data.get("rate", _DEFAULT_PARTICIPATION_RATE),
                cooldown_seconds=participation_data.get(
                    "cooldown_seconds", _DEFAULT_PARTICIPATION_COOLDOWN
                ),
                streak_max=participation_data.get("streak_max", _DEFAULT_PARTICIPATION_STREAK_MAX),
                voice_note_rate=participation_data.get("voice_note_rate", _DEFAULT_VOICE_NOTE_RATE),
                voice_note_cooldown=participation_data.get(
                    "voice_note_cooldown", _DEFAULT_VOICE_NOTE_COOLDOWN
                ),
            ),
        )

    def _load_prompt(self) -> str:
        return load_system_prompt(
            str(self.bot_dir / "prompt.md"),
            variables={"language": self._config.language},
        )

    def display_name(self) -> str:
        return self._config.display_name

    async def run(self) -> None:
        assert self._waha is not None, "configure() must be called before run()"
        assert self._settings is not None, "configure() must be called before run()"
        self._shutting_down.clear()
        self.start_task_scheduler()

        self._print_startup_config()

        # Build the webhook app and bind the listener BEFORE the slow
        # STT / WAHA probes so the cockpit's status check and inbound
        # webhooks reach us immediately.
        self._waha_client = WahaClient(self._waha)

        app = create_webhook_app(
            hmac_key=self._waha.hmac_key,
            hmac_algorithm=self._waha.hmac_algorithm,
            webhook_path=self._waha.webhook_path,
            on_message=self._handle_message,
            on_tell=self.handle_operator,
            on_ingest=None,
            on_status=self.status_snapshot,
            on_clear=self.clear_operator_history,
            on_sleep=self.set_sleep,
            on_wake=self.set_wake,
        )
        config = uvicorn.Config(
            app,
            host=self._waha.webhook_host,
            port=self._waha.webhook_port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)

        # Probe STT health concurrently with WAHA connect.  If STT is
        # unhealthy, swap in NoopSTT — never raise.
        async def _probe_stt() -> None:
            if self._stt:
                await self._stt.start()
                if not self._stt.healthy:
                    logger.warning("whisper-server unhealthy; swapping in NoopSTT")
                    self._stt = NoopSTT()

        async def _connect() -> None:
            try:
                await self._connect_waha()
            except SystemExit as exc:
                raise BotStartupError(str(exc)) from exc

        stt_task = asyncio.create_task(_probe_stt())
        connect_task = asyncio.create_task(_connect())

        # Start serving — the port binds now.
        serve_task = asyncio.create_task(self._server.serve())

        # Wait for WAHA connect (the only remaining blocker) and STT probe.
        await asyncio.gather(connect_task, stt_task)

        if self._shutting_down.is_set():
            return
        await serve_task

    async def stop(self) -> None:
        await super().stop()
        if self._stt:
            await self._stt.stop()
        if self._waha_client:
            await self._waha_client.close()
            self._waha_client = None

        if self._server and not self._shutting_down.is_set():
            self._shutting_down.set()
            self._server.should_exit = True

    async def status_snapshot(self) -> dict:
        """Return a structured status snapshot for the ``/status`` route.

        Mirrors the old console-printing ``status()`` but returns data instead
        of printing, so the CLI can render it remotely via the run endpoint.
        The profile picture is base64-encoded so the CLI can pixel-render it.
        """
        if self._waha is None:
            return {"session": None, "account": None, "error": "bot not configured"}
        client = self._waha_client or WahaClient(self._waha)
        should_close = self._waha_client is None
        try:
            session = await client.get_session_status()
            session_info: dict | None = None
            if session:
                session_info = {
                    "name": session.get("name", "unknown"),
                    "status": session.get("status", "unknown"),
                }
                me = session.get("me")
                if me and me.get("pushName"):
                    session_info["pushName"] = me.get("pushName")

            # ``session_info["status"]`` above is WAHA's cached belief about
            # its own WhatsApp Web socket — it's only updated when WAHA's
            # internal reconnect/heartbeat logic notices a problem, so it can
            # still read "WORKING" for a while after the underlying
            # connection (e.g. the host's internet) has actually died.
            # Fetching the profile forces a real round-trip through WAHA to
            # WhatsApp, so a failure/timeout here is what actually proves the
            # session is unreachable right now.
            account: dict | None = None
            connected = False
            try:
                profile = await client.get_profile()
                if profile:
                    connected = True
                    account = {
                        "name": profile.get("name", ""),
                        "id": profile.get("id", ""),
                    }
                    picture_bytes = await client.get_profile_picture()
                    if not picture_bytes and profile.get("picture"):
                        picture_bytes = await client.download_image(profile["picture"])
                    if picture_bytes:
                        account["picture"] = base64.b64encode(picture_bytes).decode("ascii")
            except Exception as exc:
                logger.debug("Failed to load profile for status: %s", exc)

            return {
                "session": session_info,
                "account": account,
                "connected": connected,
                "sleep": self._sleep_state(),
                "tasks": await self._tasks_state(),
                "capabilities": self._capabilities_state(),
            }
        finally:
            if should_close:
                await client.close()

    def _sleep_state(self) -> dict:
        """Snapshot of sleep mode: which chats are currently asleep."""
        sleeping = self._sleep_store.all() if self._sleep_store else set()
        return {
            "enabled": self._sleep_store is not None,
            "sleeping": sorted(sleeping),
        }

    async def _tasks_state(self) -> dict:
        """Snapshot of pending and recurring scheduled tasks."""
        if self._task_store is None:
            return {"pending": 0, "recurring": 0, "items": []}
        tasks = await self._task_store.list_for()
        pending = [t for t in tasks if t.repeat == "none"]
        recurring = [t for t in tasks if t.repeat != "none"]
        return {
            "pending": len(pending),
            "recurring": len(recurring),
            "items": [
                {
                    "id": t.id,
                    "goal": t.goal,
                    "chat_id": t.chat_id,
                    "due_at": t.due_at.isoformat(),
                    "repeat": t.repeat,
                }
                for t in tasks
            ],
        }

    def _capabilities_state(self) -> dict:
        """Snapshot of enabled media/processing capabilities (flags)."""
        media = self._config.media
        stt = media.stt_enabled and self._stt is not None
        tts = bool(
            self._waha and self._waha.kokoro_enabled and media.tts_enabled and self._tts_available
        )
        return {
            "voice_to_text": stt,
            "text_to_voice": tts,
            "vision": media.image_enabled,
            "video": media.video_enabled,
            "instagram": media.instagram_enabled,
        }

    async def clear_operator_history(self) -> dict:
        """Clear the operator conversation bucket (used by the ``/clear`` route).

        Lets a chat session reset mid-run without restarting the bot. The
        agent's history is keyed by ``conversation_id``; the operator turn
        uses ``"operator"`` as its bucket (see ``handle_operator``), so we
        drop just that key and mark the store dirty so it persists.
        """
        if self._agent is None:
            return {"ok": False, "error": "bot has no agent"}
        await self._agent.clear_history("operator")
        return {"ok": True}

    async def set_sleep(self, chat_id: str) -> dict:
        """Put ``chat_id`` to sleep (used by the ``/sleep`` route).

        A sleeping bot stops speaking in that chat entirely but keeps observing
        messages so it has context when woken. Idempotent: sleeping an already
        asleep chat is a no-op.
        """
        if self._sleep_store is None:
            return {"ok": False, "error": "sleep store not configured"}
        self._sleep_store.set(chat_id, True)
        return {"ok": True, "chat_id": chat_id, "sleeping": True}

    async def set_wake(self, chat_id: str) -> dict:
        """Wake ``chat_id`` up (used by the ``/wake`` route).

        Idempotent: waking an already awake chat is a no-op. Clearing the
        sleep state is all that's needed — the next inbound message resumes
        normal handling.
        """
        if self._sleep_store is None:
            return {"ok": False, "error": "sleep store not configured"}
        self._sleep_store.set(chat_id, False)
        return {"ok": True, "chat_id": chat_id, "sleeping": False}

    def _print_startup_config(self) -> None:
        assert self._waha is not None
        assert self._settings is not None
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
        if self._config.media.stt_enabled and self._stt:
            stt_type = type(self._stt).__name__
            console.print(f"  stt      {stt_type}  [dim]{self._stt_language}[/dim]")
        if self._config.media.video_enabled and self._ffmpeg_path:
            console.print(f"  video    libx264  [dim]{self._ffmpeg_path}[/dim]")
        else:
            console.print("  video    [yellow]unavailable (ffmpeg not found)[/yellow]")
        if self._waha.kokoro_enabled and self._config.media.tts_enabled:
            if self._tts_available:
                console.print(f"  tts      kokoro  [dim]{self._tts_voice}  {self._tts_lang}[/dim]")
            else:
                console.print(
                    "  tts      [yellow]unavailable (run kai vendors install kokoro)[/yellow]"
                )
        # hmac_key is mandatory (WahaSettings.hmac_key is a required field), so
        # the webhook + /tell route are always authenticated.
        console.print("  hmac     [dim]configured[/dim]")

        if warnings:
            for w in warnings:
                console.print(f"  [yellow]! {w}[/yellow]")

    async def _show_profile(self, client: WahaClient, profile: dict | None = None) -> None:
        try:
            if profile is None:
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

            if name:
                console.print(f"  [cyan]{name}[/cyan]")
            if profile_id:
                console.print(f"  [dim]{profile_id}[/dim]")
        except Exception as exc:
            logger.debug("Failed to load profile: %s", exc)

    async def _connect_waha(self) -> None:
        assert self._waha is not None
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

            await self._show_profile(client, profile)

            public_host = self._waha.webhook_public_host or "localhost"
            webhook_url = _build_webhook_url(
                public_host, self._waha.webhook_path, self._waha.webhook_port
            )

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

    def _resolve_store_path(self, settings: Settings, suffix: str) -> Path | None:
        """Resolve a per-bot state file path anchored to the bot dir.

        Shared by the seen-IDs and sleep-state stores: both live alongside
        ``<name>.tasks.json`` (CWD-independent). Returns None when
        persistence is disabled (``tasks_folder`` unset), in which case the
        caller's store stays in-memory only.
        """
        if settings.tasks_folder is None:
            return None
        folder = Path(settings.tasks_folder)
        if not folder.is_absolute():
            folder = self.bot_dir / folder
        path = folder / f"{self.instance_id}.{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _seen_store_path(self, settings: Settings) -> Path | None:
        """Path to the seen-IDs file, or None when persistence is off."""
        return self._resolve_store_path(settings, "seen.json")

    def _sleep_store_path(self, settings: Settings) -> Path | None:
        """Path to the sleep-state file, or None when persistence is off."""
        return self._resolve_store_path(settings, "sleep.json")

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
        return lock

    async def _handle_message(self, payload: dict) -> None:
        agent = self._agent
        if agent is None:
            logger.warning("Bot %s has no agent; dropping message", self.name)
            return
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
                and self._config.media.stt_enabled
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

            # ``images``/``videos`` are collected on every inbound media message
            # (not just reply turns): the vision bytes are attached only on a
            # reply turn, but the enrichment tags (``[video attached]`` /
            # ``[video audio: ...]``) and the history placeholder must land in
            # conversation history on background/sleep turns too — mirroring how
            # voice notes are transcribed above regardless of whether the bot
            # replies. Without this, a sleeping/overheard video selfie would
            # lose its spoken words entirely, since gemma-4 has no audio
            # modality and the transcript is the only durable record of them.
            images: list[bytes] = []
            videos: list[bytes] = []
            if media and media.type == MediaType.VIDEO and self._config.media.video_enabled:
                raw = await self._resolve_media_bytes(media)
                if raw:
                    if self._ffmpeg_path:
                        compressed, wav = await asyncio.to_thread(
                            compress_video, raw, self._ffmpeg_path
                        )
                        if compressed:
                            videos.append(compressed)
                            # gemma-4 has no audio modality, so a talking-selfie
                            # video gets its words in via the text channel —
                            # reusing the existing whisper STT path on the WAV
                            # ffmpeg extracted in the same pass.
                            if wav and self._config.media.stt_enabled and self._stt:
                                transcription = await self._stt.transcribe(wav)
                                if transcription:
                                    audio_tag = f"[video audio: {transcription}]"
                                    enriched_text = (
                                        f"{audio_tag}\n{enriched_text}"
                                        if enriched_text
                                        else audio_tag
                                    )
                                else:
                                    logger.warning("Video audio transcription returned empty")
                        else:
                            # Compression failed — fall back to WAHA's JPEG
                            # thumbnail so a reply turn's vision channel still
                            # sees a frame, not nothing.
                            thumb = _extract_video_thumbnail(msg)
                            if thumb:
                                images.append(thumb)
                            else:
                                logger.warning(
                                    "Video compression failed and no thumbnail available"
                                )
                    else:
                        if len(raw) <= self._config.media.max_size_mb * 1024 * 1024:
                            videos.append(raw)
                        else:
                            logger.warning(
                                "No ffmpeg and video %d bytes > cap; using thumbnail",
                                len(raw),
                            )
                    tag = "[video attached]"
                    enriched_text = f"{tag}\n{enriched_text}" if enriched_text else tag

            replies_to_bot = self._is_reply_to_bot(msg)
            context = MessageContext(
                sender_name=meta.sender_name,
                sender_id=meta.sender_id,
                conversation_id=meta.chat_id,
                multi_party=meta.is_group,
                addressed_to_bot=meta.mentions_bot or replies_to_bot,
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
                await self._handle_sleep_mode(
                    meta,
                    context,
                    enriched_text,
                    replies_to_bot,
                    roster,
                    images=images,
                    videos=videos,
                )
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
                    # Pass both ``videos`` and ``images`` so the history
                    # placeholder records the attachment either way. ``videos``
                    # holds the compressed mp4 on success; ``images`` holds the
                    # JPEG thumbnail fallback when ffmpeg failed (so a background
                    # turn still leaves a "1 image(s), N KB" note rather than
                    # a bare ``[video attached]`` tag with no size).
                    await agent.observe(
                        enriched_text,
                        conversation_id=meta.chat_id,
                        context=context,
                        images=images or None,
                        videos=videos or None,
                    )
                self._mark_skipped(meta.chat_id)
                logger.debug("Not responding to group message: %.80s", text)
                return

            # A response is expected (summoned or organic). If the inbound
            # media matches a capability this deployment has turned off,
            # decline with a canned reply instead of spending an LLM call on
            # content we can't actually process (no transcript/vision bytes
            # were ever attached above when the flag is off). The turn is
            # still recorded via ``agent.observe`` so conversation history
            # isn't silently missing the user's message (mirrors the
            # not-summoned/not-organic skip path above).
            unsupported_reason = self._unsupported_media_reason(media)
            if unsupported_reason:
                if enriched_text.strip():
                    await agent.observe(
                        enriched_text,
                        conversation_id=meta.chat_id,
                        context=context,
                        images=images or None,
                        videos=videos or None,
                    )
                await self._send_reply(meta, roster, unsupported_reason)
                self._mark_skipped(meta.chat_id)
                return

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

            yt = await self._enrich_youtube(text)
            if yt:
                tag = f"[youtube transcript:\n  {yt}]"
                enriched_text = f"{tag}\n{enriched_text}" if enriched_text else tag

            per_chat_prompt = self._build_per_chat_prompt(meta.chat_id, meta.is_group, roster)
            extra_context_parts: list[str] = []
            if per_chat_prompt:
                extra_context_parts.append(per_chat_prompt)
            tts_note = self._tts_capability_note()
            if tts_note:
                extra_context_parts.append(tts_note)
            if organic and not summoned:
                extra_context_parts.append(
                    "Nobody addressed you directly. Only reply if you have something "
                    "genuinely worth adding; otherwise set action to 'silent'."
                )
            elif summoned and not (meta.mentions_bot or replies_to_bot) and meta.is_group:
                # Summoned by keyword/name-drop rather than a direct @-mention or
                # reply. A name-drop is only a direct address if clearly aimed at
                # the bot; otherwise the model should prefer staying quiet.
                extra_context_parts.append(
                    "You were summoned because your name was mentioned in the "
                    "message, not via a direct @-mention or a reply to you. "
                    "Re-read the message: if it is not clearly aimed at you, "
                    "set action to 'silent'."
                )
            extra_context = "\n\n".join(extra_context_parts) or None

            # DMs and hard direct addresses (mention/reply) must never ghost the
            # user — expressed structurally by *not offering* the ``silent``
            # action in the WahaAction vocabulary for this turn. A group
            # name-drop is a soft summon — the model may decline.
            hard_addressed = (not meta.is_group) or meta.mentions_bot or replies_to_bot
            allow_silence = meta.is_group and not hard_addressed
            output_cls = action_cls_for_turn(
                allow_silence=allow_silence, tts_available=self._voice_enabled()
            )

            result = await agent.chat(
                enriched_text,
                output_cls=output_cls,
                conversation_id=meta.chat_id,
                context=context,
                extra_system_context=extra_context,
                images=images or None,
                videos=videos or None,
                reply_style=REPLY_STYLE,
            )

            await self._deliver_inbound(
                result,
                meta=meta,
                roster=roster,
                enriched_text=enriched_text,
                images=images,
                videos=videos,
                context=context,
                hard_addressed=hard_addressed,
                output_cls=output_cls,
            )

    async def _deliver_inbound(
        self,
        result: ChatResult,
        *,
        meta: MessageMetadata,
        roster: dict[str, str],
        enriched_text: str,
        images: list[bytes],
        videos: list[bytes],
        context: MessageContext | None,
        hard_addressed: bool,
        output_cls,
    ) -> None:
        """Dispatch a ``ChatResult`` from an inbound turn through the action table.

        Replaces the old sequential ``is_silent_reply`` / ``has_sleep_token`` /
        ``has_tool_call_leak`` checks. There is no string to interpret — the
        model chose one of the ``WahaAction`` ``Literal`` values, and this
        table decides what to do with it.
        """
        assert self._agent is not None
        action = result.action

        if result.error:
            # A schema-validation / LLM failure. On a hard direct address the
            # user is waiting, so retry once with a nudge before giving up;
            # otherwise treat the turn as silent.
            if hard_addressed:
                logger.warning(
                    "Action turn failed for %s (%s); retrying once",
                    meta.chat_id,
                    result.error,
                )
                retry = await self._agent.chat(
                    "",
                    output_cls=output_cls,
                    conversation_id=meta.chat_id,
                    context=context,
                    extra_system_context=(
                        "Your previous turn failed to produce a valid "
                        "response. Reply now with ONLY a short plain-text "
                        "WhatsApp message answering the last message."
                    ),
                    reply_style=REPLY_STYLE,
                )
                if (
                    not retry.error
                    and retry.action.action in ("reply", "send_voice_note")
                    and retry.action.text
                ):
                    action = retry.action
                else:
                    logger.warning("Retry still failed on direct address %s", meta.chat_id)
                    console.print("[dim]> (no clean reply after retry)[/dim]")
                    await self._abort_turn(meta, enriched_text, images, videos, context)
                    return
            else:
                logger.warning(
                    "Action turn failed for %s (%s); dropping", meta.chat_id, result.error
                )
                console.print("[dim]> (dropped failed turn)[/dim]")
                await self._abort_turn(meta, enriched_text, images, videos, context)
                return

        kind = action.action

        if kind == "silent":
            logger.info("Bot chose silence for %s", meta.chat_id)
            console.print("[dim]> (silent)[/dim]")
            await self._abort_turn(meta, enriched_text, images, videos, context)
            return

        if kind == "sleep":
            ack = action.text or _SLEEP_ACK
            ack = self._post_process(ack)
            if self._sleep_store is not None:
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
                await self._send(meta.chat_id, ack, mentions or None)
            except Exception as exc:
                logger.error("Failed to send sleep ack to %s: %s", meta.chat_id, exc)
            return

        if kind in ("send_dm", "send_to_group"):
            target = action.target or meta.chat_id
            out_text = self._post_process(action.text or "")
            console.print(f"[green]>[/green]  {out_text}  [dim](to {target})[/dim]")
            if self._config.mentions_enabled:
                resolved = resolve_mentions(
                    out_text, roster, bot_ids=self._bot_ids, is_group=meta.is_group
                )
                out_text, mentions = resolved.text, resolved.mentions
            else:
                out_text, mentions = strip_mention_markup(out_text), []
            try:
                await self._send(target, out_text, mentions or None)
            except Exception as exc:
                logger.error("Failed to send to %s: %s", target, exc)
                console.print(f"[red]send failed: {target}: {exc}[/red]")
            self._mark_replied(meta.chat_id)
            return

        if kind == "console":
            # ``console`` is an operator (tell) value; on an inbound turn it
            # means the model declined to deliver to a chat. Treat as silent.
            logger.info("Bot chose console action on inbound turn for %s", meta.chat_id)
            await self._abort_turn(meta, enriched_text, images, videos, context)
            return

        # Default / "reply" or "send_voice_note": deliver the prose to the
        # origin conversation. Voice notes fall back to text when TTS is
        # unavailable or synthesis fails.
        reply = self._post_process(action.text or "")
        voice_already_attempted = False
        if kind == "send_voice_note":
            console.print(f"[green]>[/green]  {reply}  [dim](voice)[/dim]")
            sent = await self._send_voice_reply(meta.chat_id, reply)
            if sent:
                self._mark_replied(meta.chat_id)
                self._last_voice_at[meta.chat_id] = time.monotonic()
                return
            logger.info("Voice reply fell back to text for %s", meta.chat_id)
            voice_already_attempted = True
        console.print(f"[green]>[/green]  {reply}")
        await self._send_reply(meta, roster, reply)
        self._mark_replied(meta.chat_id)
        if not voice_already_attempted:
            await self._maybe_send_voice_followup(meta.chat_id, reply)

    async def _maybe_send_voice_followup(self, chat_id: str, text: str) -> None:
        """After a text reply lands, sometimes echo it as a voice note.

        The LLM rarely picks ``send_voice_note`` on its own, so this gives
        the feature a probabilistic floor: a fraction of text replies get
        an audio follow-up, gated by TTS availability and a per-chat
        cooldown so it never spams. Failures are silent — the text already
        went out, so a voice miss costs nothing.
        """
        if not self._voice_enabled():
            return
        if not self._should_send_voice_followup(chat_id):
            return
        self._last_voice_at[chat_id] = time.monotonic()
        await self._send_voice_reply(chat_id, text)

    async def _handle_sleep_mode(
        self,
        meta,
        context,
        enriched_text: str,
        replies_to_bot: bool,
        roster: dict,
        *,
        images: list[bytes] | None = None,
        videos: list[bytes] | None = None,
    ) -> None:
        """Handle an inbound message while the bot is sleeping in this chat.

        While asleep, the bot never speaks on its own. It only gets a chance
        to wake when someone directly addresses it (mention or reply), and
        even then the model decides — if it chooses ``silent`` (or stays
        ``sleep``) it stays asleep; any reply-shaped action wakes it.

        ``images``/``videos`` carry media bytes already extracted upstream
        (parallel to the voice-note transcription that runs on every inbound
        message). They are passed to ``observe`` so the history placeholder
        records the attachment, and to the wake-up ``chat`` so a reply to a
        video message actually sees the frames.
        """
        addressed = meta.mentions_bot or replies_to_bot or not meta.is_group
        if enriched_text.strip():
            if self._agent is not None:
                await self._agent.observe(
                    enriched_text,
                    conversation_id=meta.chat_id,
                    context=context,
                    images=images or None,
                    videos=videos or None,
                )
        if not addressed:
            logger.debug("Bot is sleeping in %s; observing only", meta.chat_id)
            return
        if self._agent is None:
            return
        # Directly addressed while asleep: let the model decide whether to wake
        # up. A reply-shaped action wakes it; silent/sleep keeps it asleep.
        output_cls = action_cls_for_turn(allow_silence=True, tts_available=self._voice_enabled())
        wake_context = (
            "You are currently asleep in this chat. If this message is "
            "genuinely trying to wake you, set action to 'reply' and you "
            "will wake up. If it isn't (e.g. you were merely mentioned in "
            "passing), set action to 'silent' and stay asleep."
        )
        tts_note = self._tts_capability_note()
        if tts_note:
            wake_context = f"{wake_context}\n\n{tts_note}"
        result = await self._agent.chat(
            enriched_text,
            output_cls=output_cls,
            conversation_id=meta.chat_id,
            context=context,
            extra_system_context=wake_context,
            images=images or None,
            videos=videos or None,
            reply_style=REPLY_STYLE,
        )
        action = result.action
        kind = action.action
        if result.error or kind in ("silent", "sleep", "console"):
            logger.info("Bot stays asleep for %s", meta.chat_id)
            console.print("[dim]> (still sleeping)[/dim]")
            return
        # Waking up: clear sleep and deliver the reply.
        if self._sleep_store is not None:
            self._sleep_store.set(meta.chat_id, False)
        console.print("[dim]> (woke up)[/dim]")
        if kind in ("send_dm", "send_to_group"):
            target = action.target or meta.chat_id
            out_text = self._post_process(action.text or "")
            if self._config.mentions_enabled:
                resolved = resolve_mentions(
                    out_text, roster, bot_ids=self._bot_ids, is_group=meta.is_group
                )
                out_text, mentions = resolved.text, resolved.mentions
            else:
                out_text, mentions = strip_mention_markup(out_text), []
            try:
                await self._send(target, out_text, mentions or None)
            except Exception as exc:
                logger.error("Failed to send to %s: %s", target, exc)
            self._mark_replied(meta.chat_id)
            return
        reply = self._post_process(action.text or "")
        if kind == "send_voice_note":
            console.print(f"[green]>[/green]  {reply}  [dim](voice)[/dim]")
            sent = await self._send_voice_reply(meta.chat_id, reply)
            if sent:
                self._mark_replied(meta.chat_id)
                return
            logger.info("Voice reply fell back to text for %s", meta.chat_id)
        console.print(f"[green]>[/green]  {reply}")
        await self._send_reply(meta, roster, reply)
        self._mark_replied(meta.chat_id)

    async def send_text(self, chat_id: str, text: str) -> None:
        """Send a text message into ``chat_id`` via WAHA (replies, task output, ...)."""
        console.print(f"[green]>[/green]  {text}")
        await self._send(chat_id, text)

    # --- Operator (/tell) surface ---------------------------------------
    #
    # ``handle_operator`` runs an agent turn under the isolated ``operator``
    # history bucket with a per-turn tool allowlist. The agent expresses
    # message delivery through its structured ``WahaAction`` (``send_to_group``
    # / ``send_dm``), which the bot dispatches — there is no send tool.
    # ``set_goal`` is the one side-effecting tool (when ``--persist``).
    # ``result.action`` is the typed decision; ``console`` returns the reply
    # text to the operator.

    def tell_endpoint(self) -> str | None:
        if self._waha is None:
            return None
        host = self._waha.webhook_host
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return f"http://{host}:{self._waha.webhook_port}"

    def tell_hmac_key(self) -> str | None:
        return self._waha.hmac_key if self._waha is not None else None

    def tell_hmac_algorithm(self) -> str:
        return self._waha.hmac_algorithm if self._waha is not None else "sha512"

    _OPERATOR_TURN_CONTEXT = (
        "You received an instruction from the operator (the person who runs "
        "you). You express your decision through the structured action object "
        "you return — there is no tool for sending messages.\n"
        "IMPORTANT: action values (send_to_group, send_dm, console, reply, "
        "send_voice_note, silent, sleep) are NOT tools. Never call them as "
        'functions. They are values for the "action" field in your JSON '
        "response.\n"
        "- To deliver a message to a WhatsApp chat, set action to "
        '"send_to_group" (for groups, @g.us) or "send_dm" (for DMs, '
        '@c.us). You MUST fill BOTH fields: "target" = the exact chat '
        "JID taken from the instruction (never invent or guess one), and "
        '"text" = the exact message to send (plain prose, no action tokens '
        "or field names in it). Returning a send action with an empty "
        '"target" or "text" is never correct — if the instruction gives you '
        "both, copy them verbatim into the fields.\n"
        "- To deliver a VOICE NOTE to a WhatsApp chat, set action to "
        '"send_voice_note" with "target" = the destination chat JID '
        '(same rules as send_to_group/send_dm) and "text" = the words to '
        "synthesize (plain prose, short). Use this when the instruction "
        "explicitly asks for a voice note.\n"
        "- To reply to the operator ONLY (answer a question the operator "
        "asked you directly, confirm a steering directive), set action to "
        '"console" and put your reply in "text". The console reply goes to '
        "the operator's chat interface, NOT to any WhatsApp chat — the "
        "people in WhatsApp never see it.\n"
        "CRITICAL: when the instruction tells you to answer, reply, speak, "
        "or respond 'inside' / 'in' / 'to' a specific chat (e.g. 'answer "
        "inside 123@g.us', 'reply to the group', 'tell them'), you MUST use "
        "send_to_group or send_dm with that chat as the target — NEVER "
        "console. console is only for when the operator themselves is asking "
        "you a question and wants the answer back in this chat, not "
        "delivered to WhatsApp. If the instruction mentions a chat JID and "
        "asks you to say something there, the answer is always a send "
        "action, not console.\n"
        "If the instruction is a steering directive and you have the "
        "set_goal tool, call it to permanize the goal."
    )

    async def handle_operator(self, message: str, *, persist: bool = False) -> TellResult:
        if self._agent is None:
            return TellResult(ok=False, reply="bot has no agent")
        suffix = "  [dim](goal)[/dim]" if persist else ""
        console.print(f"[magenta]< operator[/magenta]  {message}{suffix}")

        tools = self._operator_tools(persist=persist)

        # Enrich the operator message the same way inbound chat messages
        # are enriched: pre-fetch Instagram posts and YouTube
        # transcripts and inject them as tagged context. This keeps the
        # "Instagram is pre-processing, don't fetch it yourself" contract
        # intact for operator turns — the agent sees the post content via
        # the ``[instagram post: ...]`` tag and IG images on the vision
        # channel, instead of hitting Instagram (which blocks fetching)
        # via ``get_webpage_content`` and getting a 403.
        enriched = message
        images: list[bytes] = []
        ig = await self._enrich_instagram(message)
        if ig is not None:
            ig_text, ig_images = ig
            images.extend(ig_images)
            if ig_text:
                tag = f"[instagram post:\n  {ig_text}]"
                enriched = f"{tag}\n{enriched}" if enriched else tag
        yt = await self._enrich_youtube(message)
        if yt:
            tag = f"[youtube transcript:\n  {yt}]"
            enriched = f"{tag}\n{enriched}" if enriched else tag

        # Read-target wrinkle: ``get_whatsapp_history`` reads its chat_id
        # from ToolContext. If the instruction mentions an explicit JID, set
        # the context to it so the agent can read that chat's history;
        # otherwise fall back to the operator bucket.
        read_target = self._extract_chat_id(message) or "operator"
        self.set_task_context(
            chat_id=read_target, owner_id="<operator>", tz_hint=self._config.timezone
        )

        op_context = self._OPERATOR_TURN_CONTEXT
        tts_note = self._tts_capability_note()
        if tts_note:
            op_context = f"{op_context}\n\n{tts_note}"
        op_output_cls = action_cls_for_turn(
            allow_silence=True, operator=True, tts_available=self._voice_enabled()
        )

        try:
            result = await self._agent.chat(
                enriched,
                output_cls=op_output_cls,
                conversation_id="operator",
                context=MessageContext(
                    sender_name="operator",
                    sender_id="<operator>",
                    addressed_to_bot=True,
                ),
                tools=tools,
                extra_system_context=op_context,
                images=images or None,
                # No REPLY_STYLE on operator turns: the operator's instruction
                # controls what to say and how long it should be. A blanket
                # brevity constraint would truncate detailed answers (business
                # analyses, web search recaps) the operator explicitly asked for.
                reply_style=None,
                # ``send_to_group``/``send_dm``/``send_voice_note`` text is
                # addressed to the *target* chat, not the operator — don't
                # record it as an assistant reply in the operator's own history
                # bucket. ``_dispatch_operator_action`` records it in the target
                # chat's history once delivery is confirmed.
                is_delegated_action=lambda a: (
                    a.action in ("send_to_group", "send_dm", "send_voice_note")
                ),
            )
        except Exception:
            logger.exception("operator turn failed")
            return TellResult(ok=False, reply="operator turn failed")

        return await self._dispatch_operator_action(result, persist=persist)

    async def _dispatch_operator_action(self, result: ChatResult, *, persist: bool) -> TellResult:
        """Dispatch the agent's structured action for an operator turn.

        The agent does not send messages itself — it emits a ``WahaAction``
        and the bot executes it. ``send_to_group`` / ``send_dm`` are
        delivered to WhatsApp and recorded in the target chat's history
        (mirroring the inbound ``_deliver_inbound`` path); ``console`` (and
        anything else) returns the reply text to the operator verbatim.
        """
        action = result.action
        kind = action.action

        if kind in ("send_to_group", "send_dm"):
            reply = ""
            target = action.target or ""
            out_text = self._post_process(action.text or "")
            sent_ok = True
            if target and out_text:
                console.print(f"[green]>[/green]  {out_text}  [dim](to {target})[/dim]")
                try:
                    await self._send(target, out_text)
                except Exception as exc:
                    logger.error("Failed to send to %s: %s", target, exc)
                    console.print(f"[red]send failed: {target}: {exc}[/red]")
                    sent_ok = False
                else:
                    # Record the assistant turn in the target chat's history
                    # so the chat sees "Kai: <what it sent>".
                    if self._agent is not None:
                        await self._agent.record_assistant_message(target, out_text)
                snippet = out_text if len(out_text) <= 60 else out_text[:57] + "..."
                reply = f"sent to {target}: {snippet}" if sent_ok else f"failed to send to {target}"
            else:
                if not target:
                    sent_ok = False
                    reply = "send action missing target"
                elif not out_text:
                    sent_ok = False
                    reply = "send action missing text"
            return TellResult(
                ok=sent_ok and result.error is None,
                actions=[{"tool": kind, "target": target, "text": out_text, "ok": sent_ok}],
                reply=reply,
            )

        if kind == "send_voice_note":
            reply = ""
            target = action.target or ""
            out_text = self._post_process(action.text or "")
            sent_ok = True
            if target and out_text:
                console.print(f"[green]>[/green]  {out_text}  [dim](voice to {target})[/dim]")
                sent = await self._send_voice_reply(target, out_text)
                if sent:
                    if self._agent is not None:
                        await self._agent.record_assistant_message(target, out_text)
                    snippet = out_text if len(out_text) <= 60 else out_text[:57] + "..."
                    reply = f"sent voice to {target}: {snippet}"
                else:
                    # Voice synthesis/delivery failed — fall back to text so
                    # the message is not lost entirely.
                    logger.info("Operator voice reply fell back to text for %s", target)
                    try:
                        await self._send(target, out_text)
                    except Exception as exc:
                        logger.error("Failed to send voice/text fallback to %s: %s", target, exc)
                        console.print(f"[red]send failed: {target}: {exc}[/red]")
                        sent_ok = False
                    else:
                        if self._agent is not None:
                            await self._agent.record_assistant_message(target, out_text)
                        snippet = out_text if len(out_text) <= 60 else out_text[:57] + "..."
                        reply = f"sent (text fallback) to {target}: {snippet}"
            else:
                if not target:
                    sent_ok = False
                    reply = "send_voice_note missing target"
                elif not out_text:
                    sent_ok = False
                    reply = "send_voice_note missing text"
            return TellResult(
                ok=sent_ok and result.error is None,
                actions=[{"tool": kind, "target": target, "text": out_text, "ok": sent_ok}],
                reply=reply,
            )

        # ``sleep``/``silent`` have no meaning on an operator turn (there is
        # no ``meta.chat_id`` for the model to put to sleep, and "silent"
        # just means it chose not to reply) — return explicitly rather than
        # falling through to the console branch, which would otherwise echo
        # an empty/stale ``action.text`` back as if it were a normal reply.
        if kind in ("sleep", "silent"):
            return TellResult(ok=True, reply="", actions=[{"tool": kind, "ok": True}])

        # ``console`` and any other action: return the reply text to the
        # operator (the agent's own words) without delivering anywhere.
        return self._build_tell_result(result, persist=persist)

    def _operator_tools(self, *, persist: bool) -> list:
        """Build the per-turn tool allowlist for an operator turn.

        Message delivery is NOT a tool — the agent expresses it through the
        structured ``send_to_group`` / ``send_dm`` action, which the bot
        dispatches (see ``_dispatch_operator_action``). ``set_goal`` is only
        available when the operator opted in to persistence
        (``persist=True``) — without it the agent cannot permanize anything,
        so the dangerous direction (one-off -> permanent) is structurally
        impossible. ``get_whatsapp_history`` is included when registered
        so the agent can recap a chat the instruction references.
        """
        tools: list[FunctionTool] = []
        if self._agent is not None:
            for tool in self._agent.get_tools():
                if tool.metadata.name == "get_whatsapp_history":
                    tools.append(tool)
                    break
        if persist:
            tools.append(self._build_set_goal_tool())
        return tools

    def _build_set_goal_tool(self) -> FunctionTool:
        bot = self

        async def set_goal(goal: str) -> str:
            """Set a persistent goal that shapes all of Kai's future replies.

            Only available when the operator passed ``--persist``. Phrase the
            goal as a directive Kai should follow going forward (it is injected
            into the system prompt on every turn).
            """
            goal = (goal or "").strip()
            if not goal:
                return "Error: goal text is required"
            if bot._agent is None:
                return "Error: no agent"
            bot._agent.goal_manager.set_goal(goal)
            return f"goal set: {goal}"

        return FunctionTool.from_defaults(
            fn=set_goal,
            name="set_goal",
            description=(
                "Set a persistent steering goal that shapes all future replies. "
                "Phrase it as a directive to follow going forward."
            ),
        )

    def _build_tell_result(self, result: ChatResult, *, persist: bool) -> TellResult:
        """Build the /tell envelope from a ChatResult.

        ``actions`` comes from ``result.tool_calls``; ``reply`` from
        ``result.action.text`` (the turn resolved ``console``).
        """
        actions: list[dict] = []
        for tc in result.tool_calls:
            entry: dict = {"tool": tc.name, "ok": tc.ok}
            for key in ("chat_id", "goal", "target", "text"):
                if key in tc.args:
                    value = tc.args[key]
                    if isinstance(value, str) and len(value) > 200:
                        value = value[:197] + "..."
                    entry[key] = value
            actions.append(entry)
        reply = result.action.text or ""
        ok = result.error is None
        # When the agent turn failed, surface the real error instead of the
        # generic "Sorry, I encountered an error..." placeholder the agent
        # builds — the operator needs to see what went wrong (e.g. a provider
        # 400, a schema failure) without reading server logs.
        if result.error:
            reply = f"error: {result.error}"
        return TellResult(ok=ok, actions=actions, reply=reply)

    @staticmethod
    def _extract_chat_id(text: str) -> str | None:
        """Pull an explicit WhatsApp JID out of free text, if present.

        Used to set the operator turn's read-target so ``get_whatsapp_history``
        reads the right chat. Returns the first JID-looking
        token, or ``None``.
        """
        match = re.search(r"\b\d{6,}@(?:g\.us|c\.us|lid)\b", text or "")
        return match.group(0) if match else None

    @asynccontextmanager
    async def _waha_client_ctx(self):
        """Yield a WAHA client, closing it only if we created it ad hoc.

        Reuses the long-lived ``self._waha_client`` when one is configured;
        otherwise builds a one-shot client from env defaults and closes it on
        exit. Yields ``None`` when no WAHA is configured at all (e.g. an
        unconfigured bot in tests) so callers can early-out instead of
        constructing a client that blocks on an unreachable host.
        """
        client = self._waha_client
        if client is not None:
            yield client
            return
        if self._waha is None:
            yield None
            return
        client = WahaClient(self._waha)
        try:
            yield client
        finally:
            await client.close()

    async def _send(self, chat_id: str, text: str, mentions: list[str] | None = None) -> None:
        async with self._waha_client_ctx() as client:
            if client is None:
                logger.warning(
                    "Bot %s has no WAHA client configured; dropping message to %s",
                    self.name,
                    chat_id,
                )
                return
            await client.send_message(chat_id, text, mentions=mentions)

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
            await self._send(meta.chat_id, out_text, mentions or None)
        except Exception as exc:
            logger.error("Failed to send reply to %s: %s", meta.chat_id, exc)
            console.print(f"[red]send failed: {meta.chat_id}: {exc}[/red]")

    def _unsupported_media_reason(self, media: MediaAttachment | None) -> str | None:
        """Canned decline text when inbound *media* matches a disabled capability.

        Called only on turns where a reply is already expected (summoned or
        organic). Lets the bot decline gracefully without spending an
        LLM/vision/STT API call on content this deployment has turned off —
        the corresponding enrichment block earlier in ``_handle_message``
        never attaches bytes or a tag for the media when its flag is off, so
        without this the model would otherwise be asked to react to an
        attachment it was never shown. Returns ``None`` when the media is
        missing or its capability is enabled.
        """
        if media is None:
            return None
        if media.type in (MediaType.VOICE, MediaType.AUDIO) and not (
            self._config.media.stt_enabled and self._stt
        ):
            return (
                "Voice notes aren't supported on this bot right now — "
                "could you send that as text instead?"
            )
        if media.type == MediaType.IMAGE and not self._config.media.image_enabled:
            return (
                "Images aren't supported on this bot right now — "
                "could you describe it in text instead?"
            )
        if media.type == MediaType.VIDEO and not self._config.media.video_enabled:
            return (
                "Videos aren't supported on this bot right now — "
                "could you describe it in text instead?"
            )
        return None

    def _tts_capability_note(self) -> str:
        """Per-turn context steering the agent on ``send_voice_note`` limits.

        Two independent gates, each with its own advisory:

        1. TTS offline: the action schema already omits ``send_voice_note``
           when TTS is offline (see :func:`action_cls_for_turn`'s
           ``tts_available`` gate), so this note is a *fallback* advisory for
           the rare case the model still leans toward voice.
        2. TTS online but the bot's own configured language has no Kokoro
           voice at all (``self._tts_lang is None``, e.g. a German-language
           bot — see :func:`kai.bots.waha.tts.resolve_kokoro_lang`): voice
           notes still work for replies Kokoro can detect as one of its
           supported languages, but not as a *fallback* for the bot's own
           inconclusive-language replies. The model needs the supported-
           language list to know this rather than silently attempting (and
           failing) a voice note, or promising one it can't deliver.

        Returns "" only when voice notes are fully available with no caveats.
        """
        if not self._voice_enabled():
            return (
                "Voice notes are currently unavailable (TTS is offline). Do "
                "not use the `send_voice_note` action — deliver the same text "
                "via `reply` (or `send_dm` / `send_to_group` on an operator "
                "turn) instead."
            )
        if self._tts_lang is None:
            supported = ", ".join(SUPPORTED_KOKORO_LANGUAGE_NAMES)
            return (
                f"Voice notes only work in these languages: {supported}. "
                f"This bot's configured language ({self._config.language}) is "
                "not one of them, so `send_voice_note` may fail for short or "
                "ambiguous replies in that language — prefer `reply` unless "
                "the reply text is clearly one of the supported languages."
            )
        return ""

    def _voice_enabled(self) -> bool:
        """True when Kokoro TTS is configured and currently reachable.

        Mirrors ``_send_voice_reply``'s gate so the action schema, the
        per-turn advisory, and the delivery path all agree on whether
        ``send_voice_note`` is a real option this turn.
        """
        return bool(self._waha is not None and self._waha.kokoro_enabled and self._tts_available)

    async def _send_voice_reply(self, chat_id: str, text: str) -> bool:
        """Synthesize *text* to a voice note and send it via ``/api/sendVoice``.

        The reply's language is detected from *text* so a bot can answer in
        any language and still get a matching Kokoro voice. When the detected
        language is not supported by Kokoro v1.0 (e.g. Cyrillic, Arabic,
        Korean), synthesis is skipped and the caller falls back to text.

        The prompt instructs the model to match the incoming message's
        language per turn (see ``prompt.md``), so a single chat can move
        between languages — the bot's static configured language is not
        necessarily *this* reply's language. ``detect_kokoro_lang`` only
        needs a fallback when the reply text itself is too short/ambiguous
        to score any language's stopwords (e.g. "OK!", "Listo."); for those,
        the *conversation's own* last confidently-detected language is a far
        better guess than the deployment's static default — a short ack in
        an otherwise-Spanish chat is far more likely to still be Spanish than
        to have switched to the bot's configured English. The static
        ``self._tts_lang`` is only used as the final fallback for a chat with
        no voice history yet.

        Returns ``True`` on success. Returns ``False`` when TTS is
        unavailable, the text exceeds ``kokoro_max_chars``, the language is
        unsupported, or synthesis / delivery fails — the caller should fall
        back to a text reply in that case.
        """
        if not self._tts_available or self._waha is None or not self._waha.kokoro_enabled:
            return False
        clean = strip_mention_markup(text).strip()
        if not clean or len(clean) > self._waha.kokoro_max_chars:
            return False
        lang = self._detect_voice_lang(chat_id, clean)
        if lang is None:
            logger.info("Skipping voice reply: unsupported language in %r", clean[:60])
            return False
        voice = resolve_kokoro_voice(lang, overrides=self._voice_map)
        if voice is None:
            logger.info("Skipping voice reply: no Kokoro voice for lang %s", lang)
            return False
        try:
            audio = await asyncio.to_thread(
                synthesize,
                text=clean,
                host=self._waha.kokoro_server_host,
                port=self._waha.kokoro_server_port,
                voice=voice,
                lang=lang,
                speed=self._waha.kokoro_speed,
            )
        except Exception as exc:
            logger.warning("Voice synthesis failed, falling back to text: %s", exc)
            return False
        if audio is None:
            return False
        async with self._waha_client_ctx() as client:
            if client is None:
                return False
            try:
                await client.send_voice(chat_id, audio)
                return True
            except Exception as exc:
                logger.warning("Voice send failed, falling back to text: %s", exc)
                return False

    def _detect_voice_lang(self, chat_id: str, clean_text: str) -> str | None:
        """Resolve the Kokoro lang code to synthesize *clean_text* in.

        First checks whether *clean_text* itself confidently identifies a
        language (script detection or a stopword match) with no fallback
        bias at all. Only when that's inconclusive does it fall back — to
        this chat's own last confidently-detected voice language first, then
        to the bot's static configured language. A confident detection here
        is remembered for the chat so the next ambiguous reply in the same
        conversation inherits it too.

        Han text without kana (kanji-only) is deliberately excluded from the
        "no fallback bias" pass: it's genuinely ambiguous between Japanese
        and Mandarin, and ``detect_kokoro_lang`` resolves that ambiguity
        entirely via its ``fallback`` argument (see its Han branch and
        ``test_kanji_only_japanese_honors_configured_lang``). Calling it with
        ``fallback=None`` doesn't make that branch unbiased — it just forces
        the internal default fallback to ``"en-us"``, which always loses the
        ja/cmn tie-break to ``"cmn"``. Treating that as a "confident" result
        would silently overwrite a chat correctly remembered as Japanese
        (e.g. from an earlier kana-containing reply) the next time it sends
        a kanji-only ack.
        """
        has_kana = any(0x3040 <= ord(ch) < 0x3100 for ch in clean_text)
        has_han = any(0x4E00 <= ord(ch) < 0x9FFF for ch in clean_text)
        if has_han and not has_kana:
            chat_fallback = self._last_voice_lang.get(chat_id, self._tts_lang)
            lang = detect_kokoro_lang(clean_text, fallback=chat_fallback)
            if lang is not None:
                _bounded_dict_set(self._last_voice_lang, chat_id, lang, _VOICE_LANG_MAX)
            return lang
        confident = detect_kokoro_lang(clean_text, fallback=None)
        if confident is not None:
            _bounded_dict_set(self._last_voice_lang, chat_id, confident, _VOICE_LANG_MAX)
            return confident
        chat_fallback = self._last_voice_lang.get(chat_id, self._tts_lang)
        lang = detect_kokoro_lang(clean_text, fallback=chat_fallback)
        if lang is not None:
            _bounded_dict_set(self._last_voice_lang, chat_id, lang, _VOICE_LANG_MAX)
        return lang

    async def _resolve_media_bytes(self, media: MediaAttachment) -> bytes | None:
        if media.data is not None:
            return media.data
        if media.url:
            async with self._waha_client_ctx() as client:
                if client is None:
                    return None
                return await client.download_media(media.url, self._config.media.max_size_mb)
        return None

    async def _fetch_message_media(self, chat_id: str, message_id: str) -> dict | None:
        """Re-fetch a message from WAHA with downloadMedia=true."""
        async with self._waha_client_ctx() as client:
            if client is None:
                return None
            try:
                return await client.get_message(chat_id, message_id, download_media=True)
            except Exception as exc:
                logger.warning("Failed to fetch media for message %s: %s", message_id, exc)
                return None

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

    async def _enrich_youtube(self, text: str) -> str | None:
        """If text contains a YouTube URL, return a transcript summary string.

        Mirrors ``_enrich_instagram``: fetches the transcript off-thread and
        returns a tagged text block for the agent (no images). Returns None
        when there's nothing to enrich or the fetch failed, so the message
        still reaches the agent untouched.
        """
        video_id = extract_youtube_video_id(text)
        if video_id is None:
            return None
        try:
            result = await asyncio.to_thread(fetch_youtube_transcript, video_id)
        except Exception as exc:
            logger.warning("YouTube fetch failed for %s: %s", video_id, exc)
            return None
        if result.get("error") or not result.get("transcript_text"):
            return None
        lines = [
            f"title-language: {result.get('language', 'unknown')}",
            f"video_id: {video_id}",
            f"url: {result.get('url', f'https://www.youtube.com/watch?v={video_id}')}",
            "transcript:",
            result["transcript_text"],
        ]
        return "\n  ".join(lines)

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

    def _should_send_voice_followup(self, chat_id: str) -> bool:
        """Delegate to the extracted voice-followup probability check."""
        cfg = self._config.participation
        return should_send_voice_followup(
            chat_id,
            voice_note_rate=cfg.voice_note_rate,
            voice_note_cooldown=cfg.voice_note_cooldown,
            last_voice_at=self._last_voice_at,
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
        videos: list[bytes],
        context: MessageContext | None,
    ) -> None:
        """Drop the current reply: mark skipped, observe the inbound message so
        the conversation history still reflects it, then end the turn.

        Shared by the tool-call-leak and silence drop paths so their behavior
        can't drift apart.
        """
        self._mark_skipped(meta.chat_id)
        if enriched_text.strip() and self._agent is not None:
            await self._agent.observe(
                enriched_text,
                conversation_id=meta.chat_id,
                context=context,
                images=images or None,
                videos=videos or None,
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

        async with self._waha_client_ctx() as client:
            if client is None:
                return
            try:
                participants = await client.get_chat_participants(chat_id)
            except Exception as exc:
                logger.warning("Failed to fetch participants for %s: %s", chat_id, exc)
                return

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
                reply_name = sanitize_display_name(
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
            if (
                u
                and "instagram.com" not in u
                and "youtube.com" not in u
                and "youtu.be" not in u
                and u not in urls
            ):
                urls.append(u)
        if urls:
            parts.append(f"[links in message: {', '.join(urls)}]")

        if parts:
            return "\n".join(parts) + "\n" + text
        return text
