import asyncio
import time
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from kai.agent.core import ChatResult
from kai.bots.waha import Bot, BotConfig, MediaConfig, ParticipationConfig, _build_webhook_url
from kai.bots.waha.actions import (
    _FULL_ACTIONS,
    WahaAction,
    WahaNoSilentAction,
    WahaNoSilentNoVoiceAction,
)
from kai.bots.waha.history import register_chat_history_tool
from kai.bots.waha.processing import should_send_voice_followup
from kai.bots.waha.seen_store import SeenStore
from kai.bots.waha.sleep_store import SleepStore
from kai.templates.schema import TemplateDef


def _chat_result(text: str | None = "reply", *, action: str = "reply", target: str | None = None):
    """Build a ChatResult the way the agent would after a waha turn."""
    return ChatResult(
        reply=text or "",
        tool_calls=[],
        action=WahaAction(action=cast(_FULL_ACTIONS, action), text=text, target=target),
    )


def _make_bot(config: BotConfig | None = None, bot_dir: Path | None = None) -> Bot:
    bot = Bot(bot_dir=bot_dir or Path("."), config=config or BotConfig())
    # In-memory stores so tests don't need configure() / a data dir; mirrors
    # the in-process behavior of the former dict[str, bool] / set[str].
    bot._seen_store = SeenStore(None, max_size=2048)
    bot._sleep_store = SleepStore(None)
    return bot


def _plain_template(config: dict | None = None) -> TemplateDef:
    """A minimal waha template — empty config unless given (BotConfig defaults apply)."""
    return TemplateDef(
        name="test",
        transport="waha",
        display_name="T",
        description="T",
        actions=["reply"],
        config=config or {},
    )


class TestShouldRespond:
    def test_dm_always_responds(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        assert bot._should_respond("hello", is_group=False, mentions_bot=False) is True

    def test_group_with_keyword(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        assert bot._should_respond("hey kai what's up", is_group=True, mentions_bot=False) is True

    def test_group_without_keyword(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        assert bot._should_respond("just chatting", is_group=True, mentions_bot=False) is False

    def test_group_reply_to_bot_responds(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        assert (
            bot._should_respond(
                "just chatting", is_group=True, mentions_bot=False, replies_to_bot=True
            )
            is True
        )

    def test_group_media_only_without_mention_skipped(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        assert bot._should_respond("", is_group=True, mentions_bot=False, has_media=True) is False

    def test_group_media_only_with_mention_responds(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        assert bot._should_respond("", is_group=True, mentions_bot=True, has_media=True) is True

    def test_group_media_with_keyword_in_text_responds(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        assert (
            bot._should_respond("hey kai", is_group=True, mentions_bot=False, has_media=True)
            is True
        )

    def test_dm_media_only_responds(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        assert bot._should_respond("", is_group=False, mentions_bot=False, has_media=True) is True

    def test_group_with_mention(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        assert bot._should_respond("hello", is_group=True, mentions_bot=True) is True

    def test_case_insensitive_keyword(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        assert bot._should_respond("Hey kAI", is_group=True, mentions_bot=False) is True

    def test_empty_keyword_responds_to_all_group_messages(self):
        bot = _make_bot(BotConfig(trigger_keyword=""))
        assert bot._should_respond("anything", is_group=True, mentions_bot=False) is True

    def test_whitespace_keyword_responds_to_all_group_messages(self):
        bot = _make_bot(BotConfig(trigger_keyword="  "))
        assert bot._should_respond("anything", is_group=True, mentions_bot=False) is True


class TestIsReplyToBot:
    def test_reply_to_bot_detected(self):
        bot = _make_bot()
        bot._bot_ids.add("12345@c.us")
        msg = {"replyTo": {"participant": "12345@c.us"}}
        assert bot._is_reply_to_bot(msg) is True

    def test_reply_to_bot_with_lid(self):
        bot = _make_bot()
        bot._bot_ids.add("12345@lid")
        msg = {"replyTo": {"participant": "12345@c.us"}}
        assert bot._is_reply_to_bot(msg) is True

    def test_reply_to_non_bot(self):
        bot = _make_bot()
        bot._bot_ids.add("12345@c.us")
        msg = {"replyTo": {"participant": "99999@c.us"}}
        assert bot._is_reply_to_bot(msg) is False

    def test_no_reply(self):
        bot = _make_bot()
        bot._bot_ids.add("12345@c.us")
        assert bot._is_reply_to_bot({}) is False

    def test_reply_with_dict_participant(self):
        bot = _make_bot()
        bot._bot_ids.add("12345@c.us")
        msg = {"replyTo": {"participant": {"_serialized": "12345@c.us"}}}
        assert bot._is_reply_to_bot(msg) is True

    def test_reply_with_empty_participant(self):
        bot = _make_bot()
        bot._bot_ids.add("12345@c.us")
        msg = {"replyTo": {"participant": ""}}
        assert bot._is_reply_to_bot(msg) is False


class TestLearnBotIdentity:
    def test_adopts_lid_when_phone_matches(self):
        bot = _make_bot()
        bot._bot_ids.add("4917600000000@c.us")
        bot._learn_bot_identity("4917600000000@c.us", "98765432109876@lid")
        assert "98765432109876@lid" in bot._bot_ids
        # The learned LID makes a reply addressed by LID resolve to the bot.
        assert bot._is_reply_to_bot({"replyTo": {"participant": "98765432109876@lid"}}) is True

    def test_ignores_other_participants(self):
        bot = _make_bot()
        bot._bot_ids.add("4917600000000@c.us")
        bot._learn_bot_identity("99999@c.us", "88888@lid")
        assert "88888@lid" not in bot._bot_ids

    def test_noop_when_no_known_identity(self):
        bot = _make_bot()
        bot._learn_bot_identity("4917600000000@c.us", "98765432109876@lid")
        assert "98765432109876@lid" not in bot._bot_ids

    def test_handles_missing_lid(self):
        bot = _make_bot()
        bot._bot_ids.add("4917600000000@c.us")
        bot._learn_bot_identity("4917600000000@c.us", None)
        assert bot._bot_ids == {"4917600000000@c.us"}


class TestHasToolCallLeak:
    """Leaked tool-call markup can no longer reach the chat.

    The terminal step is now a schema-constrained ``WahaAction`` (see
    ``kai.bots.waha.actions``), so there is no free-text channel for raw
    tool-call markup to leak into. The old ``has_tool_call_leak`` detector is
    gone; these tests just assert prose still passes through ``post_process``
    unchanged (the cleaning path that remains).
    """

    def test_clean_reply_passes_through(self):
        assert _make_bot()._post_process("sure, that movie comes out friday") == (
            "sure, that movie comes out friday"
        )

    def test_empty_is_empty(self):
        assert _make_bot()._post_process("") == ""


class TestPostProcess:
    def test_strips_bold(self):
        assert _make_bot()._post_process("**hello**") == "hello"

    def test_strips_italic(self):
        assert _make_bot()._post_process("*hello*") == "hello"

    def test_strips_underscore(self):
        assert _make_bot()._post_process("_hello_") == "hello"

    def test_strips_bullet_points(self):
        # List markers are removed and lines collapsed into one prose line.
        assert _make_bot()._post_process("- item one\n- item two") == "item one item two"

    def test_strips_hashtags(self):
        assert _make_bot()._post_process("hello #world #tag") == "hello"

    def test_strips_single_emoji(self):
        # Emojis are stripped post-hoc: the prompt's default is "no emoji", and
        # small models over-use them. A reply is always plain prose.
        assert _make_bot()._post_process("hello 😭") == "hello"

    def test_strips_variation_selector_emoji(self):
        # "❤️" is U+2764 + U+FE0F: both the symbol and its variation selector
        # are removed, leaving no leftover codepoints.
        assert _make_bot()._post_process("ok ❤️") == "ok"

    def test_strips_zwj_emoji(self):
        # ZWJ family emoji is a single grapheme cluster — pictographs and the
        # joiner are all removed.
        family = "👨\u200d👩\u200d👧"
        assert _make_bot()._post_process(f"nice {family}") == "nice"

    def test_strips_all_emojis(self):
        # Multiple emojis collapse to plain text with no doubled spaces.
        assert _make_bot()._post_process("hi 😭😂🔥") == "hi"

    def test_no_emoji_preserved(self):
        assert _make_bot()._post_process("hello world") == "hello world"

    def test_combined_markdown_and_emojis(self):
        result = _make_bot()._post_process("**hello** 😭😂 world")
        assert "**" not in result
        # Markdown stripped and emojis removed — plain prose only.
        assert result == "hello world"

    def test_strips_multiple_hashtags(self):
        assert _make_bot()._post_process("#one #two #three done") == "done"

    def test_whitespace_only(self):
        assert _make_bot()._post_process("   ") == ""

    def test_preserves_plain_text(self):
        assert _make_bot()._post_process("just a normal message") == "just a normal message"

    def test_strips_bold_inside_sentence(self):
        result = _make_bot()._post_process("this is **bold** and this is normal")
        assert result == "this is bold and this is normal"

    def test_strips_trailing_period_on_short_reply(self):
        assert _make_bot()._post_process("Vale.") == "Vale"
        assert _make_bot()._post_process("ya voy.") == "ya voy"

    def test_keeps_trailing_question_and_exclamation(self):
        assert _make_bot()._post_process("¿qué dices?") == "¿qué dices?"
        assert _make_bot()._post_process("¡claro!") == "¡claro!"

    def test_keeps_ellipsis(self):
        assert _make_bot()._post_process("ya veremos...") == "ya veremos..."

    def test_drops_trailing_period_on_single_sentence_even_when_long(self):
        # The trailing-period rule is sentence-based, not length-based: a
        # single-sentence reply loses its lone trailing period regardless of
        # length. (The period is just stiff punctuation in casual chat.)
        long_reply = (
            "Esta es una respuesta suficientemente larga como para conservar su punto final"
        )
        assert _make_bot()._post_process(long_reply + ".") == long_reply

    def test_keeps_period_on_multisentence_short_reply(self):
        # Internal periods are preserved; only a lone trailing period is dropped.
        assert _make_bot()._post_process("sí. claro") == "sí. claro"

    def test_strips_wrapping_backticks(self):
        # Models mirror code-span formatting from prompts and wrap the whole
        # reply in backticks — these must never reach WhatsApp.
        assert _make_bot()._post_process("`hello world`") == "hello world"
        assert (
            _make_bot()._post_process("` @[Sara] the supreme court`") == "@[Sara] the supreme court"
        )

    def test_strips_inline_backtick_spans(self):
        assert _make_bot()._post_process("use `code` here") == "use code here"

    def test_strips_numbered_list_markers(self):
        assert _make_bot()._post_process("1. one\n2. two") == "one two"

    def test_collapses_newlines_into_one_line(self):
        assert _make_bot()._post_process("line one\nline two\nline three") == (
            "line one line two line three"
        )

    def test_strips_markdown_links(self):
        assert _make_bot()._post_process("see [Lisbon weather](https://x.io/lisbon)") == (
            "see Lisbon weather"
        )


class TestLoadConfig:
    def test_parses_config_json(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"trigger_keyword": "kai", "language": "Spanish"}')
        bot = _make_bot(bot_dir=tmp_path)
        config = bot._load_config(_plain_template(), config_path=config_file)
        assert config.trigger_keyword == "kai"
        assert config.language == "Spanish"

    def test_parses_timezone(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"trigger_keyword": "kai", "timezone": "America/Santo_Domingo"}')
        bot = _make_bot(bot_dir=tmp_path)
        config = bot._load_config(_plain_template(), config_path=config_file)
        assert config.timezone == "America/Santo_Domingo"

    def test_timezone_defaults_none(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"trigger_keyword": "kai"}')
        bot = _make_bot(bot_dir=tmp_path)
        config = bot._load_config(_plain_template(), config_path=config_file)
        assert config.timezone is None

    def test_missing_config_returns_defaults(self, tmp_path):
        missing = tmp_path / "config.json"
        bot = _make_bot(bot_dir=tmp_path)
        config = bot._load_config(_plain_template(), config_path=missing)
        assert config == BotConfig()

    def test_media_config_defaults(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"trigger_keyword": "kai"}')
        bot = _make_bot(bot_dir=tmp_path)
        config = bot._load_config(_plain_template(), config_path=config_file)
        assert config.media.image_enabled is True
        assert config.media.stt_enabled is True
        assert config.media.tts_enabled is True
        assert config.media.max_size_mb == 10

    def test_media_config_custom(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(
            '{"trigger_keyword": "kai", "media": '
            '{"image_enabled": false, "stt_enabled": false, '
            '"tts_enabled": true, "max_size_mb": 5}}'
        )
        bot = _make_bot(bot_dir=tmp_path)
        config = bot._load_config(_plain_template(), config_path=config_file)
        assert config.media.image_enabled is False
        assert config.media.stt_enabled is False
        assert config.media.tts_enabled is True
        assert config.media.max_size_mb == 5

    def test_template_config_applies_when_no_config_file(self, tmp_path):
        # No config.json → BotConfig defaults ← template.config. A template
        # setting temperature/participation shapes the baseline config.
        tmpl = _plain_template(config={"temperature": 0.7, "participation": {"rate": 0.45}})
        bot = _make_bot(bot_dir=tmp_path)
        config = bot._load_config(tmpl, config_path=tmp_path / "missing.json")
        assert config.temperature == 0.7
        assert config.participation.rate == 0.45

    def test_config_file_overrides_template(self, tmp_path):
        # config.json wins over template.config (per-deployment override).
        tmpl = _plain_template(config={"temperature": 0.7})
        config_file = tmp_path / "config.json"
        config_file.write_text('{"temperature": 0.2}')
        bot = _make_bot(bot_dir=tmp_path)
        config = bot._load_config(tmpl, config_path=config_file)
        assert config.temperature == 0.2

    def test_config_file_partial_merge_keeps_template(self, tmp_path):
        # A config.json that sets only one nested field keeps the template's
        # other nested fields (deep merge, not replace).
        tmpl = _plain_template(config={"participation": {"rate": 0.45, "cooldown_seconds": 45}})
        config_file = tmp_path / "config.json"
        config_file.write_text('{"participation": {"rate": 0.9}}')
        bot = _make_bot(bot_dir=tmp_path)
        config = bot._load_config(tmpl, config_path=config_file)
        assert config.participation.rate == 0.9
        assert config.participation.cooldown_seconds == 45


class TestConfigResolution:
    def test_external_config_takes_precedence(self, tmp_path, monkeypatch):
        from kai.config.settings import Settings

        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        external = configs_dir / "waha.json"
        external.write_text('{"trigger_keyword": "kai", "language": "Spanish"}')

        packaged = tmp_path / "config.json"
        packaged.write_text('{"trigger_keyword": "kai", "language": "English"}')

        monkeypatch.setattr(
            "kai.bots.base.get_settings",
            lambda: Settings.for_test(configs_dir=configs_dir),
        )
        bot = _make_bot(bot_dir=tmp_path)
        path = bot.resolve_config_path()
        assert path == external

    def test_no_packaged_fallback_returns_none(self, tmp_path, monkeypatch):
        """There is no packaged-default fallback — only the external override
        is ever resolved; anything else (e.g. a config.json shipped
        alongside the bot) is ignored."""
        from kai.config.settings import Settings

        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        (tmp_path / "config.json").write_text('{"trigger_keyword": "kai"}')

        monkeypatch.setattr(
            "kai.bots.base.get_settings",
            lambda: Settings.for_test(configs_dir=configs_dir),
        )
        bot = _make_bot(bot_dir=tmp_path)
        path = bot.resolve_config_path()
        assert path is None

    def test_returns_none_when_no_config_exists(self, tmp_path, monkeypatch):
        from kai.config.settings import Settings

        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        monkeypatch.setattr(
            "kai.bots.base.get_settings",
            lambda: Settings.for_test(configs_dir=configs_dir),
        )
        bot = _make_bot(bot_dir=tmp_path)
        assert bot.resolve_config_path() is None

    def test_load_config_uses_external_first(self, tmp_path, monkeypatch):
        from kai.config.settings import Settings

        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        external = configs_dir / "waha.json"
        external.write_text(
            '{"trigger_keyword": "kai", "whitelist": ["group@g.us"], "language": "Spanish"}'
        )

        monkeypatch.setattr(
            "kai.bots.base.get_settings",
            lambda: Settings.for_test(configs_dir=configs_dir),
        )
        bot = _make_bot(bot_dir=tmp_path)
        config = bot._load_config(_plain_template())
        assert config.whitelist == ["group@g.us"]
        assert config.language == "Spanish"


def _make_chat_history_bot():
    """Create a bot with a registered get_whatsapp_history tool, return (bot, tool, mock_client)."""
    bot = _make_bot(BotConfig(trigger_keyword="kai"))
    from kai.agent.context import ToolContext

    bot._tool_context = ToolContext(chat_id="group@g.us")
    mock_client = MagicMock()
    mock_client.get_chat_messages = AsyncMock()
    bot._waha_client = mock_client

    captured: list = []

    class FakeAgent:
        def register_tool(self, tool):
            captured.append(tool)

    register_chat_history_tool(FakeAgent(), bot=bot)  # type: ignore[arg-type]
    return bot, captured[0], mock_client


async def _call_tool(tool, **kwargs):
    """Invoke a FunctionTool async and return its string content."""
    output = await tool.acall(**kwargs)
    return str(output.content)


class TestGetChatHistory:
    @pytest.mark.asyncio
    async def test_formats_messages_chronological(self):
        bot, tool, client = _make_chat_history_bot()
        # WAHA returns newest-first; tool reverses to oldest-first.
        client.get_chat_messages.return_value = [
            {"id": "m2", "body": "second", "fromMe": False, "participant": "12345678902@c.us"},
            {"id": "m1", "body": "first", "fromMe": False, "participant": "12345678901@c.us"},
        ]
        result = await _call_tool(tool, limit=50)
        lines = result.split("\n")
        assert lines[0] == "[12345678901] first"
        assert lines[1] == "[12345678902] second"

    @pytest.mark.asyncio
    async def test_labels_bot_messages_as_kai(self):
        bot, tool, client = _make_chat_history_bot()
        client.get_chat_messages.return_value = [
            {"id": "m1", "body": "my reply", "fromMe": True},
            {"id": "m0", "body": "hello", "fromMe": False, "participant": "12345678901@c.us"},
        ]
        result = await _call_tool(tool)
        assert "[kAI] my reply" in result
        assert "[12345678901] hello" in result

    @pytest.mark.asyncio
    async def test_resolves_sender_from_roster(self):
        bot, tool, client = _make_chat_history_bot()
        bot._rosters["group@g.us"] = {"12345678901@c.us": "Juan"}
        client.get_chat_messages.return_value = [
            {"id": "m1", "body": "hola", "fromMe": False, "participant": "12345678901@c.us"},
        ]
        result = await _call_tool(tool)
        assert result == "[Juan] hola"

    @pytest.mark.asyncio
    async def test_falls_back_to_notify_name(self):
        bot, tool, client = _make_chat_history_bot()
        client.get_chat_messages.return_value = [
            {
                "id": "m1",
                "body": "hi",
                "fromMe": False,
                "participant": "12345678901@c.us",
                "_data": {"notifyName": "Juan Palotes"},
            },
        ]
        result = await _call_tool(tool)
        assert result == "[Juan Palotes] hi"

    @pytest.mark.asyncio
    async def test_falls_back_to_top_level_notify_name(self):
        bot, tool, client = _make_chat_history_bot()
        # Some WAHA versions put notifyName at the top level, not in _data.
        client.get_chat_messages.return_value = [
            {
                "id": "m1",
                "body": "hi",
                "fromMe": False,
                "participant": "12345678901@c.us",
                "notifyName": "Andrei",
            },
        ]
        result = await _call_tool(tool)
        assert result == "[Andrei] hi"

    @pytest.mark.asyncio
    async def test_falls_back_to_data_author_dict(self):
        bot, tool, client = _make_chat_history_bot()
        # When participant is absent, _data.author (dict form) is the sender.
        client.get_chat_messages.return_value = [
            {
                "id": "m1",
                "body": "hello",
                "fromMe": False,
                "from": "group@g.us",
                "_data": {
                    "author": {"_serialized": "12345678901@c.us"},
                    "notifyName": "Carlos",
                },
            },
        ]
        result = await _call_tool(tool)
        assert result == "[Carlos] hello"

    @pytest.mark.asyncio
    async def test_falls_back_to_data_author_string(self):
        bot, tool, client = _make_chat_history_bot()
        client.get_chat_messages.return_value = [
            {
                "id": "m1",
                "body": "hello",
                "fromMe": False,
                "from": "group@g.us",
                "_data": {"author": "12345678901@c.us", "notifyName": "Lucerna"},
            },
        ]
        result = await _call_tool(tool)
        assert result == "[Lucerna] hello"

    @pytest.mark.asyncio
    async def test_falls_back_to_from_for_dm(self):
        bot, tool, client = _make_chat_history_bot()
        # In a DM, participant is absent and 'from' is the other person's JID.
        client.get_chat_messages.return_value = [
            {"id": "m1", "body": "hey", "fromMe": False, "from": "12345678901@c.us"},
        ]
        result = await _call_tool(tool)
        assert result == "[12345678901] hey"

    @pytest.mark.asyncio
    async def test_falls_back_to_phone_digits(self):
        bot, tool, client = _make_chat_history_bot()
        client.get_chat_messages.return_value = [
            {
                "id": "m1",
                "body": "hi",
                "fromMe": False,
                "participant": "18091234567@c.us",
            },
        ]
        result = await _call_tool(tool)
        assert result == "[18091234567] hi"

    @pytest.mark.asyncio
    async def test_sanitizes_name_with_brackets(self):
        bot, tool, client = _make_chat_history_bot()
        client.get_chat_messages.return_value = [
            {
                "id": "m1",
                "body": "test",
                "fromMe": False,
                "participant": "12345678901@c.us",
                "_data": {"notifyName": "Bob [Admin]"},
            },
        ]
        result = await _call_tool(tool)
        # Brackets stripped so they don't corrupt the [Name] body format.
        assert result == "[Bob Admin] test"

    @pytest.mark.asyncio
    async def test_sanitizes_name_with_newline(self):
        bot, tool, client = _make_chat_history_bot()
        client.get_chat_messages.return_value = [
            {
                "id": "m1",
                "body": "test",
                "fromMe": False,
                "participant": "12345678901@c.us",
                "_data": {"notifyName": "Multi\nLine"},
            },
        ]
        result = await _call_tool(tool)
        assert "\n" not in result.split("]")[0]
        assert "[Multi Line] test" == result

    @pytest.mark.asyncio
    async def test_skips_empty_body_messages(self):
        bot, tool, client = _make_chat_history_bot()
        client.get_chat_messages.return_value = [
            {"id": "m1", "body": "real msg", "fromMe": False, "participant": "12345678901@c.us"},
            {"id": "m0", "body": "", "fromMe": False, "participant": "12345678902@c.us"},
            {"id": "m0b", "body": "   ", "fromMe": False, "participant": "12345678903@c.us"},
        ]
        result = await _call_tool(tool)
        assert result == "[12345678901] real msg"

    @pytest.mark.asyncio
    async def test_returns_message_when_no_messages_found(self):
        bot, tool, client = _make_chat_history_bot()
        client.get_chat_messages.return_value = []
        result = await _call_tool(tool)
        assert result == "No text messages found."

    @pytest.mark.asyncio
    async def test_clamps_limit_and_offset(self):
        bot, tool, client = _make_chat_history_bot()
        client.get_chat_messages.return_value = []
        await _call_tool(tool, limit=500, offset=-10)
        call_kwargs = client.get_chat_messages.call_args.kwargs
        assert call_kwargs["limit"] == 200
        assert call_kwargs["offset"] == 0

    @pytest.mark.asyncio
    async def test_passes_limit_and_offset_to_client(self):
        bot, tool, client = _make_chat_history_bot()
        client.get_chat_messages.return_value = []
        await _call_tool(tool, limit=100, offset=50)
        call_kwargs = client.get_chat_messages.call_args.kwargs
        assert call_kwargs["limit"] == 100
        assert call_kwargs["offset"] == 50

    @pytest.mark.asyncio
    async def test_returns_error_string_on_client_exception(self):
        bot, tool, client = _make_chat_history_bot()
        client.get_chat_messages.side_effect = RuntimeError("WAHA down")
        result = await _call_tool(tool)
        assert result.startswith("Error: could not fetch chat history")
        assert "WAHA down" in result

    @pytest.mark.asyncio
    async def test_returns_error_when_no_chat_context(self):
        from kai.agent.context import ToolContext

        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._tool_context = ToolContext(chat_id="")
        captured: list = []

        class FakeAgent:
            def register_tool(self, tool):
                captured.append(tool)

        register_chat_history_tool(FakeAgent(), bot=bot)  # type: ignore[arg-type]
        result = await _call_tool(captured[0])
        assert result == "Error: no chat context available"

    @pytest.mark.asyncio
    async def test_creates_and_closes_client_when_none(self, monkeypatch):
        bot, tool, client = _make_chat_history_bot()
        # Simulate no persistent client; a temporary one must be created & closed.
        bot._waha_client = None
        bot._waha = MagicMock()
        mock_client = MagicMock()
        mock_client.get_chat_messages = AsyncMock(return_value=[])
        mock_client.close = AsyncMock()
        monkeypatch.setattr("kai.bots.waha.client.WahaClient", lambda *a, **_kw: mock_client)
        result = await _call_tool(tool)
        assert result == "No text messages found."
        mock_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_close_persistent_client_on_success(self):
        bot, tool, client = _make_chat_history_bot()
        client.get_chat_messages.return_value = [
            {"id": "m1", "body": "msg", "fromMe": False, "participant": "12345678901@c.us"},
        ]
        await _call_tool(tool)
        client.close.assert_not_called()


class TestBuildWebhookUrl:
    def test_plain_host(self):
        assert _build_webhook_url("192.168.1.254:8000", "/webhook/waha") == (
            "http://192.168.1.254:8000/webhook/waha"
        )

    def test_host_with_http_scheme(self):
        assert _build_webhook_url("http://tunnel.example.com", "/webhook/waha") == (
            "http://tunnel.example.com/webhook/waha"
        )

    def test_host_with_https_scheme(self):
        assert _build_webhook_url("https://tunnel.example.com", "/webhook/waha") == (
            "https://tunnel.example.com/webhook/waha"
        )

    def test_host_with_scheme_and_port(self):
        assert _build_webhook_url("https://tunnel.example.com:8443", "/webhook/waha") == (
            "https://tunnel.example.com:8443/webhook/waha"
        )

    def test_host_with_scheme_drops_path_component(self):
        assert _build_webhook_url("https://tunnel.example.com/extra", "/webhook/waha") == (
            "https://tunnel.example.com/webhook/waha"
        )

    def test_bare_host_with_port_arg_appends_port(self):
        """Regression: a bare host (e.g. a docker-compose service name like
        'cockpit') with no embedded port must get webhook_port appended, or
        WAHA silently POSTs webhooks to the default port 80 where nothing is
        listening."""
        assert _build_webhook_url("cockpit", "/webhook/whatsapp-1", 8123) == (
            "http://cockpit:8123/webhook/whatsapp-1"
        )

    def test_bare_host_without_port_arg_omits_port(self):
        assert _build_webhook_url("cockpit", "/webhook/whatsapp-1") == (
            "http://cockpit/webhook/whatsapp-1"
        )

    def test_host_with_embedded_port_ignores_port_arg(self):
        assert _build_webhook_url("192.168.1.254:8000", "/webhook/waha", 9999) == (
            "http://192.168.1.254:8000/webhook/waha"
        )

    def test_scheme_host_ignores_port_arg(self):
        assert _build_webhook_url("https://tunnel.example.com", "/webhook/waha", 9999) == (
            "https://tunnel.example.com/webhook/waha"
        )


class TestPerChatLock:
    def test_returns_same_lock_for_same_chat(self):
        bot = _make_bot()
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            lock_a = bot._get_chat_lock("chat-1")
            lock_b = bot._get_chat_lock("chat-1")
            assert lock_a is lock_b
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    def test_returns_distinct_locks_for_distinct_chats(self):
        bot = _make_bot()
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            lock_a = bot._get_chat_lock("chat-1")
            lock_b = bot._get_chat_lock("chat-2")
            assert lock_a is not lock_b
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    @pytest.mark.asyncio
    async def test_send_order_serialized_per_chat(self, monkeypatch):
        """Two rapid messages in one chat must be processed in arrival order."""
        from unittest.mock import AsyncMock, MagicMock

        bot = _make_bot(BotConfig(trigger_keyword=""))
        # Avoid a real roster refresh (network) so the turn reaches chat()
        # immediately — this test is about chat-lock serialization, not rosters.
        monkeypatch.setattr(bot, "_refresh_group_roster", AsyncMock())
        agent = MagicMock()
        # First chat() call blocks until released, second must wait.
        gate = asyncio.Event()
        order: list[str] = []

        async def chat_stub(message, **kwargs):
            if not order:
                order.append(f"start:{message}")
                await gate.wait()
                order.append(f"end:{message}")
            else:
                order.append(f"start:{message}")
                order.append(f"end:{message}")
            return _chat_result("reply")

        agent.chat = AsyncMock(side_effect=chat_stub)
        agent.observe = AsyncMock()
        bot._agent = agent
        bot._bot_ids.add("bot@c.us")
        bot._config.mentions_enabled = False
        bot._send = AsyncMock()

        def make_payload(body: str) -> dict:
            return {
                "event": "message",
                "payload": {
                    "id": body,
                    "from": "chat-1@g.us",
                    "participant": "123@lid",
                    "body": body,
                    "type": "chat",
                    "_data": {"notifyName": "Sender", "author": "123@lid"},
                },
            }

        t1 = asyncio.ensure_future(bot._handle_message(make_payload("first")))
        await asyncio.sleep(0)  # let t1 enter the lock + chat()
        await asyncio.sleep(0.05)
        t2 = asyncio.ensure_future(bot._handle_message(make_payload("second")))
        await asyncio.sleep(0.05)
        # t2 must be blocked behind t1 (which is still gated).
        assert order == ["start:first"]
        gate.set()
        await asyncio.gather(t1, t2)
        assert order == ["start:first", "end:first", "start:second", "end:second"]


def _group_payload(body: str, *, chat_id: str = "group@g.us", msg_id: str | None = None) -> dict:
    return {
        "event": "message",
        "payload": {
            "id": msg_id or body,
            "from": chat_id,
            "participant": "123@lid",
            "body": body,
            "type": "chat",
            "_data": {"notifyName": "Sender", "author": "123@lid"},
        },
    }


class TestToolCallRendering:
    def test_render_tool_call_prints_name_and_preview(self, capsys):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._render_tool_call("web_search", {"query": "fifa 2026"}, "England 1-0 Croatia")
        out = capsys.readouterr().out
        assert "web_search" in out
        assert "fifa 2026" in out
        assert "England 1-0 Croatia" in out

    def test_render_tool_call_truncates_long_args_and_result(self, capsys):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        long_query = "x" * 200
        long_result = "y" * 300
        bot._render_tool_call("get_webpage_content", {"url": long_query}, long_result)
        out = capsys.readouterr().out
        assert "..." in out
        assert long_query not in out
        assert long_result not in out

    def test_render_tool_call_handles_empty_result(self, capsys):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._render_tool_call("get_weather", {"location": "Berlin"}, "")
        out = capsys.readouterr().out
        assert "get_weather" in out
        assert "Berlin" in out


class TestReasoningChannelStripping:
    """Reasoning-model "channel" tokens must never leak into a sent message.

    The terminal structured step strips reasoning channels from ``action.text``
    before handing it back, so the bot never sees them; these tests confirm a
    silent action stays silent and a reply action carries clean prose.
    """

    @pytest.mark.asyncio
    async def test_silent_action_sends_nothing(self, monkeypatch):
        bot = _make_bot(
            BotConfig(
                trigger_keyword="kai",
                participation=ParticipationConfig(
                    enabled=True, rate=1.0, cooldown_seconds=0, streak_max=5
                ),
            )
        )
        bot._send = AsyncMock()
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result(None, action="silent"))
        agent.observe = AsyncMock()
        bot._agent = agent
        monkeypatch.setattr("random.random", lambda: 0.9)

        await bot._handle_message(_group_payload("just chatting"))

        # Silent: nothing sent.
        bot._send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reply_action_carries_clean_prose(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._send = AsyncMock()
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("hey what's up"))
        agent.observe = AsyncMock()
        bot._agent = agent

        await bot._handle_message(_group_payload("hey kai"))

        assert bot._send.await_args is not None
        sent_text = bot._send.await_args.args[1]
        assert "channel" not in sent_text
        assert sent_text == "hey what's up"


class TestSleepToken:
    """The ``sleep`` action drives the sleep state — the model decides, not regex."""

    @pytest.mark.asyncio
    async def test_sleep_action_sets_sleeping_and_sends_reply(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._send = AsyncMock()
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("ok, heading off", action="sleep"))
        agent.observe = AsyncMock()
        bot._agent = agent

        await bot._handle_message(_group_payload("kai go to sleep"))

        assert bot._sleep_store is not None
        assert bot._sleep_store.is_sleeping("group@g.us") is True
        bot._send.assert_awaited_once()
        sent = bot._send.call_args.args[1]
        assert "heading off" in sent

    @pytest.mark.asyncio
    async def test_sleep_action_with_no_text_uses_default_ack(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._send = AsyncMock()
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result(None, action="sleep"))
        agent.observe = AsyncMock()
        bot._agent = agent

        await bot._handle_message(_group_payload("kai go to sleep"))

        assert bot._sleep_store is not None
        assert bot._sleep_store.is_sleeping("group@g.us") is True
        sent = bot._send.call_args.args[1]
        assert sent == "going quiet, ping me if you need me"

    @pytest.mark.asyncio
    async def test_sleeping_bot_observes_non_addressed_without_llm(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        assert bot._sleep_store is not None
        bot._sleep_store.mark("group@g.us", True)
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("should not happen"))
        agent.observe = AsyncMock()
        bot._agent = agent

        # Plain group message, nobody mentions or replies to the bot.
        await bot._handle_message(_group_payload("just chatting"))

        agent.chat.assert_not_awaited()
        agent.observe.assert_awaited()
        assert bot._sleep_store.is_sleeping("group@g.us") is True

    @pytest.mark.asyncio
    async def test_sleeping_bot_wakes_on_mention_with_real_reply(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        assert bot._sleep_store is not None
        bot._sleep_store.mark("group@g.us", True)
        bot._send = AsyncMock()
        bot._bot_ids.add("bot@c.us")
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("hey, I'm back"))
        agent.observe = AsyncMock()
        bot._agent = agent

        payload = _group_payload("@bot you there?", msg_id="m1")
        payload["payload"]["_data"]["mentionedJidList"] = ["bot@c.us"]
        await bot._handle_message(payload)

        assert bot._sleep_store.is_sleeping("group@g.us") is False
        agent.chat.assert_awaited_once()
        sent = bot._send.call_args.args[1]
        assert sent == "hey, I'm back"

    @pytest.mark.asyncio
    async def test_sleeping_bot_stays_asleep_on_silent_reply(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        assert bot._sleep_store is not None
        bot._sleep_store.mark("group@g.us", True)
        bot._bot_ids.add("bot@c.us")
        bot._send = AsyncMock()
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result(None, action="silent"))
        agent.observe = AsyncMock()
        bot._agent = agent

        payload = _group_payload("@bot ping", msg_id="m1")
        payload["payload"]["_data"]["mentionedJidList"] = ["bot@c.us"]
        await bot._handle_message(payload)

        assert bot._sleep_store.is_sleeping("group@g.us") is True
        bot._send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sleeping_dm_wakes_bot(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        assert bot._sleep_store is not None
        bot._sleep_store.mark("123@c.us", True)
        bot._send = AsyncMock()
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("hi again"))
        agent.observe = AsyncMock()
        bot._agent = agent

        payload = {
            "event": "message",
            "payload": {
                "id": "dm1",
                "from": "123@c.us",
                "body": "hello?",
                "type": "chat",
                "_data": {"notifyName": "Friend"},
            },
        }
        await bot._handle_message(payload)

        assert bot._sleep_store.is_sleeping("123@c.us") is False
        bot._send.assert_awaited_once()


class TestOrganicParticipation:
    def test_disabled_participation_never_offers(self):
        bot = _make_bot(
            BotConfig(
                trigger_keyword="kai",
                participation=ParticipationConfig(enabled=False, rate=1.0),
            )
        )
        assert bot._should_organically_participate("g@g.us", "anything", is_group=True) is False

    def test_cooldown_blocks_offer(self):
        bot = _make_bot(
            BotConfig(
                trigger_keyword="kai",
                participation=ParticipationConfig(
                    enabled=True, rate=1.0, cooldown_seconds=60, streak_max=5
                ),
            )
        )
        # Force a recent reply.
        bot._last_reply_at["g@g.us"] = time.monotonic()
        assert bot._should_organically_participate("g@g.us", "anything", is_group=True) is False

    def test_streak_max_blocks_offer(self):
        bot = _make_bot(
            BotConfig(
                trigger_keyword="kai",
                participation=ParticipationConfig(
                    enabled=True, rate=1.0, cooldown_seconds=0, streak_max=2
                ),
            )
        )
        bot._consecutive_replies["g@g.us"] = 2
        assert bot._should_organically_participate("g@g.us", "anything", is_group=True) is False

    def test_dm_never_organic(self):
        bot = _make_bot(
            BotConfig(
                trigger_keyword="kai",
                participation=ParticipationConfig(enabled=True, rate=1.0),
            )
        )
        assert bot._should_organically_participate("123@c.us", "hi", is_group=False) is False

    def test_mark_replied_increments_streak(self):
        bot = _make_bot()
        bot._mark_replied("g@g.us")
        bot._mark_replied("g@g.us")
        assert bot._consecutive_replies["g@g.us"] == 2

    def test_mark_skipped_resets_streak(self):
        bot = _make_bot()
        bot._mark_replied("g@g.us")
        bot._mark_skipped("g@g.us")
        assert bot._consecutive_replies["g@g.us"] == 0

    def test_inactive_chat_uses_full_cooldown(self):
        # No prior reply (streak 0): the normal cooldown applies unchanged.
        bot = _make_bot(
            BotConfig(
                trigger_keyword="kai",
                participation=ParticipationConfig(
                    enabled=True, rate=1.0, cooldown_seconds=100, streak_max=5
                ),
            )
        )
        bot._last_reply_at["g@g.us"] = time.monotonic() - 50.0
        # streak stays 0 (last turn was a skip / never replied)
        assert bot._should_organically_participate("g@g.us", "anything", is_group=True) is False

    def test_active_exchange_relaxes_cooldown(self):
        # Bot replied last (streak 1); a follow-up past the relaxed window but
        # still inside the normal cooldown is no longer blocked.
        bot = _make_bot(
            BotConfig(
                trigger_keyword="kai",
                participation=ParticipationConfig(
                    enabled=True, rate=1.0, cooldown_seconds=100, streak_max=5
                ),
            )
        )
        bot._consecutive_replies["g@g.us"] = 1
        bot._last_reply_at["g@g.us"] = time.monotonic() - 50.0  # 30 < 50 < 100
        assert bot._should_organically_participate("g@g.us", "anything", is_group=True) is True

    def test_active_exchange_still_blocks_within_relaxed_cooldown(self):
        # Even in an active exchange, a too-fast follow-up is throttled.
        bot = _make_bot(
            BotConfig(
                trigger_keyword="kai",
                participation=ParticipationConfig(
                    enabled=True, rate=1.0, cooldown_seconds=100, streak_max=5
                ),
            )
        )
        bot._consecutive_replies["g@g.us"] = 1
        bot._last_reply_at["g@g.us"] = time.monotonic() - 10.0  # 10 < 30 relaxed
        assert bot._should_organically_participate("g@g.us", "anything", is_group=True) is False

    def test_active_exchange_rate_boost_applies(self, monkeypatch):
        # With a zero base rate, a quick follow-up in an active exchange still
        # gets a chance thanks to the boost.
        bot = _make_bot(
            BotConfig(
                trigger_keyword="kai",
                participation=ParticipationConfig(
                    enabled=True, rate=0.0, cooldown_seconds=100, streak_max=5
                ),
            )
        )
        bot._consecutive_replies["g@g.us"] = 1
        bot._last_reply_at["g@g.us"] = time.monotonic() - 50.0  # within normal window
        monkeypatch.setattr("random.random", lambda: 0.3)
        assert bot._should_organically_participate("g@g.us", "anything", is_group=True) is True

    def test_no_rate_boost_after_long_gap(self, monkeypatch):
        # After the normal cooldown window has fully elapsed, the boost no
        # longer applies — only the base rate matters.
        bot = _make_bot(
            BotConfig(
                trigger_keyword="kai",
                participation=ParticipationConfig(
                    enabled=True, rate=0.0, cooldown_seconds=100, streak_max=5
                ),
            )
        )
        bot._consecutive_replies["g@g.us"] = 1
        bot._last_reply_at["g@g.us"] = time.monotonic() - 200.0  # past normal cooldown
        monkeypatch.setattr("random.random", lambda: 0.3)
        assert bot._should_organically_participate("g@g.us", "anything", is_group=True) is False

    @pytest.mark.asyncio
    async def test_non_summoned_group_message_offers_when_rate_one(self, monkeypatch):
        bot = _make_bot(
            BotConfig(
                trigger_keyword="kai",
                participation=ParticipationConfig(
                    enabled=True, rate=1.0, cooldown_seconds=100, streak_max=5
                ),
            )
        )
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result(None, action="silent"))
        agent.observe = AsyncMock()
        bot._agent = agent
        monkeypatch.setattr("random.random", lambda: 0.9)

        await bot._handle_message(_group_payload("just chatting about pizza"))

        # Offered to the model (which chose silence).
        agent.chat.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_summoned_group_message_skipped_when_rate_zero(self, monkeypatch):
        bot = _make_bot(
            BotConfig(
                trigger_keyword="kai",
                participation=ParticipationConfig(
                    enabled=True, rate=0.0, cooldown_seconds=0, streak_max=5
                ),
            )
        )
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("nope"))
        agent.observe = AsyncMock()
        bot._agent = agent
        monkeypatch.setattr("random.random", lambda: 0.5)

        await bot._handle_message(_group_payload("just chatting about pizza"))

        agent.chat.assert_not_awaited()
        agent.observe.assert_awaited()


class TestShouldSendVoiceFollowup:
    """Pure-function tests for the extracted voice-followup probability check."""

    def test_zero_rate_never_offers(self):
        assert (
            should_send_voice_followup(
                "g@g.us", voice_note_rate=0.0, voice_note_cooldown=0, last_voice_at={}
            )
            is False
        )

    def test_cooldown_blocks_offer(self):
        last_voice_at = {"g@g.us": time.monotonic()}
        assert (
            should_send_voice_followup(
                "g@g.us",
                voice_note_rate=1.0,
                voice_note_cooldown=300,
                last_voice_at=last_voice_at,
            )
            is False
        )

    def test_cooldown_elapsed_allows_offer(self, monkeypatch):
        last_voice_at = {"g@g.us": time.monotonic() - 301.0}
        monkeypatch.setattr("random.random", lambda: 0.5)
        assert (
            should_send_voice_followup(
                "g@g.us",
                voice_note_rate=1.0,
                voice_note_cooldown=300,
                last_voice_at=last_voice_at,
            )
            is True
        )

    def test_never_offered_chat_ignores_cooldown(self, monkeypatch):
        monkeypatch.setattr("random.random", lambda: 0.5)
        assert (
            should_send_voice_followup(
                "new@g.us", voice_note_rate=1.0, voice_note_cooldown=300, last_voice_at={}
            )
            is True
        )


class TestVoiceFollowup:
    """Bot-level tests for the probabilistic voice-note echo after a text reply."""

    def _dm_payload(self, body: str = "hello") -> dict:
        return {
            "event": "message",
            "payload": {
                "id": "dm1",
                "from": "123@c.us",
                "body": body,
                "type": "chat",
                "_data": {"notifyName": "Friend"},
            },
        }

    def _voice_ready_bot(self, *, voice_note_rate: float, voice_note_cooldown: int = 0) -> Bot:
        bot = _make_bot(
            BotConfig(
                trigger_keyword="kai",
                participation=ParticipationConfig(
                    voice_note_rate=voice_note_rate, voice_note_cooldown=voice_note_cooldown
                ),
            )
        )
        bot._waha = MagicMock()
        bot._waha.kokoro_enabled = True
        bot._tts_available = True
        bot._send = AsyncMock()
        return bot

    @pytest.mark.asyncio
    async def test_text_reply_gets_voice_followup_when_offered(self, monkeypatch):
        bot = self._voice_ready_bot(voice_note_rate=1.0)
        send_with_retry = cast(AsyncMock, bot._send)
        bot._send_voice_reply = AsyncMock(return_value=True)
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("hi there"))
        agent.observe = AsyncMock()
        bot._agent = agent
        monkeypatch.setattr("random.random", lambda: 0.0)

        await bot._handle_message(self._dm_payload())

        send_with_retry.assert_awaited_once()
        bot._send_voice_reply.assert_awaited_once_with("123@c.us", "hi there")
        assert "123@c.us" in bot._last_voice_at

    @pytest.mark.asyncio
    async def test_text_reply_skips_voice_followup_when_tts_unavailable(self, monkeypatch):
        bot = self._voice_ready_bot(voice_note_rate=1.0)
        send_with_retry = cast(AsyncMock, bot._send)
        bot._tts_available = False
        bot._send_voice_reply = AsyncMock(return_value=True)
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("hi there"))
        agent.observe = AsyncMock()
        bot._agent = agent
        monkeypatch.setattr("random.random", lambda: 0.0)

        await bot._handle_message(self._dm_payload())

        send_with_retry.assert_awaited_once()
        bot._send_voice_reply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_text_reply_skips_voice_followup_when_rate_zero(self, monkeypatch):
        bot = self._voice_ready_bot(voice_note_rate=0.0)
        bot._send_voice_reply = AsyncMock(return_value=True)
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("hi there"))
        agent.observe = AsyncMock()
        bot._agent = agent
        monkeypatch.setattr("random.random", lambda: 0.0)

        await bot._handle_message(self._dm_payload())

        bot._send_voice_reply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_voice_note_falls_back_to_text_without_retrying_followup(
        self, monkeypatch
    ):
        # When the model's own send_voice_note fails, the turn already falls
        # back to text. The probabilistic followup must not fire again for
        # the same reply — that would just retry the same failing synthesis.
        bot = self._voice_ready_bot(voice_note_rate=1.0)
        send_with_retry = cast(AsyncMock, bot._send)
        bot._send_voice_reply = AsyncMock(return_value=False)
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("hi there", action="send_voice_note"))
        agent.observe = AsyncMock()
        bot._agent = agent
        monkeypatch.setattr("random.random", lambda: 0.0)

        await bot._handle_message(self._dm_payload())

        send_with_retry.assert_awaited_once()
        bot._send_voice_reply.assert_awaited_once_with("123@c.us", "hi there")

    @pytest.mark.asyncio
    async def test_failed_followup_still_starts_cooldown(self, monkeypatch):
        # A failed synthesis/delivery attempt must still record last_voice_at,
        # otherwise a chat with consistently failing TTS (e.g. replies always
        # over kokoro_max_chars) gets re-rolled and re-synthesized on every
        # single text reply forever, defeating the cooldown entirely.
        bot = self._voice_ready_bot(voice_note_rate=1.0, voice_note_cooldown=300)
        bot._send_voice_reply = AsyncMock(return_value=False)
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("hi there"))
        agent.observe = AsyncMock()
        bot._agent = agent
        monkeypatch.setattr("random.random", lambda: 0.0)

        await bot._handle_message(self._dm_payload())

        bot._send_voice_reply.assert_awaited_once_with("123@c.us", "hi there")
        assert "123@c.us" in bot._last_voice_at

        # Second reply arrives immediately after: cooldown must block a
        # second synthesis attempt even though the first one failed.
        await bot._handle_message(self._dm_payload())

        bot._send_voice_reply.assert_awaited_once()


class TestDetectVoiceLang:
    """``_detect_voice_lang`` — per-chat language memory for ambiguous replies.

    The model matches the incoming message's language per turn (see
    prompt.md), so a single chat can move between languages. An ambiguous
    reply (no script/stopword signal, e.g. "OK!") must inherit *this chat's*
    last confidently-detected language rather than the bot's static
    configured ``_tts_lang`` — otherwise a short ack in an otherwise-Spanish
    chat gets synthesized with the bot's default English voice.
    """

    def test_confident_text_is_used_directly_and_remembered(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._tts_lang = "en-us"

        lang = bot._detect_voice_lang("chat@c.us", "Hola, ¿cómo estás? Que bueno verte.")

        assert lang == "es"
        assert bot._last_voice_lang["chat@c.us"] == "es"

    def test_ambiguous_reply_inherits_chat_history_over_static_default(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._tts_lang = "en-us"
        # Chat was previously confidently detected as Spanish.
        bot._detect_voice_lang("chat@c.us", "Hola, ¿cómo estás? Que bueno verte.")

        lang = bot._detect_voice_lang("chat@c.us", "OK!")

        assert lang == "es"

    def test_ambiguous_reply_with_no_chat_history_uses_static_default(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._tts_lang = "en-us"

        lang = bot._detect_voice_lang("new-chat@c.us", "OK!")

        assert lang == "en-us"

    def test_ambiguous_reply_updates_chat_memory(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._tts_lang = "fr-fr"

        bot._detect_voice_lang("chat@c.us", "OK!")

        assert bot._last_voice_lang["chat@c.us"] == "fr-fr"

    def test_different_chats_keep_independent_language_memory(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._tts_lang = "en-us"
        bot._detect_voice_lang("es-chat@c.us", "Hola, ¿cómo estás? Que bueno verte.")
        bot._detect_voice_lang("fr-chat@c.us", "Bonjour, comment ça va? C'est très bien.")

        assert bot._detect_voice_lang("es-chat@c.us", "OK!") == "es"
        assert bot._detect_voice_lang("fr-chat@c.us", "OK!") == "fr-fr"

    def test_unsupported_script_returns_none_and_does_not_poison_memory(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._tts_lang = "en-us"
        bot._detect_voice_lang("chat@c.us", "Hola, ¿cómo estás? Que bueno verte.")

        lang = bot._detect_voice_lang("chat@c.us", "Привет, как дела?")

        assert lang is None
        # Chat memory still holds the last real signal (Spanish), not
        # clobbered by an unsupported-script turn that returned None.
        assert bot._last_voice_lang["chat@c.us"] == "es"

    def test_kanji_only_reply_honors_chat_remembered_as_japanese(self):
        # Han-only text (kanji, no kana) is genuinely ambiguous between
        # Japanese and Mandarin — detect_kokoro_lang resolves that via its
        # fallback, not via script alone. A chat previously established as
        # Japanese (e.g. from an earlier kana-containing reply) must keep
        # using "ja" for a terse kanji-only ack like "了解", not silently
        # flip to "cmn" because the ack itself has no kana.
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._tts_lang = "en-us"
        bot._detect_voice_lang("chat@c.us", "こんにちは、元気ですか?")
        assert bot._last_voice_lang["chat@c.us"] == "ja"

        lang = bot._detect_voice_lang("chat@c.us", "了解")

        assert lang == "ja"
        assert bot._last_voice_lang["chat@c.us"] == "ja"

    def test_kanji_only_reply_with_no_chat_history_uses_static_default(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._tts_lang = "ja"

        lang = bot._detect_voice_lang("new-chat@c.us", "了解")

        assert lang == "ja"


class TestGroupRosterRefresh:
    @pytest.mark.asyncio
    async def test_group_message_triggers_roster_refresh(self, monkeypatch):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result(None, action="silent"))
        agent.observe = AsyncMock()
        bot._agent = agent
        bot._bot_ids.add("bot@c.us")

        called: list[str] = []

        async def fake_refresh(chat_id, roster):
            called.append(chat_id)

        monkeypatch.setattr(bot, "_refresh_group_roster", fake_refresh)

        await bot._handle_message(_group_payload("hey kai", chat_id="g@g.us"))

        assert called == ["g@g.us"]

    @pytest.mark.asyncio
    async def test_dm_does_not_refresh_roster(self, monkeypatch):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("hi"))
        agent.observe = AsyncMock()
        bot._agent = agent

        called = False

        async def fake_refresh(chat_id, roster):
            nonlocal called
            called = True

        monkeypatch.setattr(bot, "_refresh_group_roster", fake_refresh)

        await bot._handle_message(
            {
                "event": "message",
                "payload": {
                    "id": "dm1",
                    "from": "123@c.us",
                    "body": "hello kai",
                    "type": "chat",
                    "_data": {"notifyName": "Friend"},
                },
            }
        )
        assert called is False

    @pytest.mark.asyncio
    async def test_canonicalizes_to_pn_and_carries_name_by_digits(self):
        # A member known by @lid (from a message) gets carried over to the @c.us
        # (pn) canonical key via digit-prefix match.
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        roster: dict[str, str] = {"111@lid": "Alice"}
        client = MagicMock()
        client.get_chat_participants = AsyncMock(
            return_value=[{"id": "111@lid", "pn": "111@c.us", "role": "participant"}]
        )
        bot._waha_client = client

        await bot._refresh_group_roster("g@g.us", roster)

        assert roster == {"111@c.us": "Alice"}
        assert bot._roster_refreshed_at["g@g.us"] > 0

    @pytest.mark.asyncio
    async def test_nameless_lurkers_not_added(self):
        # Participants with no known name are NOT added (avoids phone-digit
        # pollution of the prompt roster).
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        roster: dict[str, str] = {}
        client = MagicMock()
        client.get_chat_participants = AsyncMock(
            return_value=[
                {"id": "111@lid", "pn": "111@c.us", "role": "participant"},
                {"id": "222@c.us", "pn": "222@c.us", "role": "participant"},
            ]
        )
        bot._waha_client = client

        await bot._refresh_group_roster("g@g.us", roster)

        assert roster == {}

    @pytest.mark.asyncio
    async def test_records_admins(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        roster: dict[str, str] = {"111@c.us": "Alice", "222@c.us": "Bob"}
        client = MagicMock()
        client.get_chat_participants = AsyncMock(
            return_value=[
                {"id": "111@c.us", "pn": "111@c.us", "role": "admin"},
                {"id": "222@c.us", "pn": "222@c.us", "role": "participant"},
            ]
        )
        bot._waha_client = client

        await bot._refresh_group_roster("g@g.us", roster)

        assert bot._group_admins["g@g.us"] == {"111@c.us"}

    @pytest.mark.asyncio
    async def test_prunes_left_members(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        roster: dict[str, str] = {"111@c.us": "Alice", "222@c.us": "Bob"}
        client = MagicMock()
        client.get_chat_participants = AsyncMock(
            return_value=[
                {"id": "111@c.us", "pn": "111@c.us", "role": "participant"},
                {"id": "222@c.us", "pn": "222@c.us", "role": "left"},
            ]
        )
        bot._waha_client = client

        await bot._refresh_group_roster("g@g.us", roster)

        assert "222@c.us" not in roster
        assert roster["111@c.us"] == "Alice"

    @pytest.mark.asyncio
    async def test_preserves_lid_entries_when_digits_differ(self):
        # Regression: inbound messages key members by an opaque @lid while the
        # participants endpoint returns @c.us phone JIDs with unrelated digit
        # prefixes. The @lid entry (the one mention resolution needs) must
        # survive a refresh instead of being wiped.
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        roster: dict[str, str] = {"12345678901234@lid": "Juan Palotes"}
        client = MagicMock()
        client.get_chat_participants = AsyncMock(
            return_value=[
                {"id": "4917600000000@c.us", "pn": "4917600000000@c.us", "role": "admin"},
                {"id": "18090000000@c.us", "pn": "18090000000@c.us", "role": "participant"},
            ]
        )
        bot._waha_client = client

        await bot._refresh_group_roster("g@g.us", roster)

        assert roster == {"12345678901234@lid": "Juan Palotes"}
        assert bot._group_admins["g@g.us"] == {"4917600000000@c.us"}

    @pytest.mark.asyncio
    async def test_refresh_respects_ttl(self, monkeypatch):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._roster_refreshed_at["g@g.us"] = time.monotonic()  # just refreshed
        client = MagicMock()
        client.get_chat_participants = AsyncMock(return_value=[])
        bot._waha_client = client

        await bot._refresh_group_roster("g@g.us", {})

        client.get_chat_participants.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refresh_failure_does_not_raise(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        roster: dict[str, str] = {"111@c.us": "Alice"}
        client = MagicMock()
        client.get_chat_participants = AsyncMock(side_effect=RuntimeError("boom"))
        bot._waha_client = client

        # Must not raise; roster is left untouched.
        await bot._refresh_group_roster("g@g.us", roster)
        assert roster == {"111@c.us": "Alice"}


class TestPerChatPromptAdmins:
    def test_includes_admins_when_present(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._group_admins["g@g.us"] = {"111@c.us"}
        roster = {"111@c.us": "Alice", "222@c.us": "Bob"}
        prompt = bot._build_per_chat_prompt("g@g.us", is_group=True, roster=roster)
        assert prompt is not None
        assert "People in this chat: Alice, Bob" in prompt
        assert "Admins: Alice" in prompt

    def test_omits_admins_line_when_none(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        roster = {"111@c.us": "Alice"}
        prompt = bot._build_per_chat_prompt("g@g.us", is_group=True, roster=roster)
        assert prompt is not None
        assert "Admins" not in prompt


class TestDMNoSilence:
    @pytest.mark.asyncio
    async def test_dm_uses_no_silent_action_vocabulary(self):
        # A DM is a hard direct address — the user must not be ghosted. This is
        # expressed structurally by passing an output_cls whose ``Literal``
        # excludes ``silent`` (no runtime flag). TTS is enabled here to isolate
        # the no-silent dimension.
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._waha = MagicMock()
        bot._waha.kokoro_enabled = True
        bot._tts_available = True
        # Voice delivery is not under test here (only the no-silent schema is);
        # mock it so a probabilistic voice-followup roll can't reach the real
        # _send_voice_reply and crash on the mocked WahaSettings attrs.
        bot._send_voice_reply = AsyncMock(return_value=False)
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("hi"))
        agent.observe = AsyncMock()
        bot._agent = agent

        payload = {
            "event": "message",
            "payload": {
                "id": "dm1",
                "from": "123@c.us",
                "body": "hello",
                "type": "chat",
                "_data": {"notifyName": "Friend"},
            },
        }
        await bot._handle_message(payload)

        agent.chat.assert_awaited_once()
        assert agent.chat.call_args.kwargs.get("output_cls") is WahaNoSilentAction

    @pytest.mark.asyncio
    async def test_dm_with_tts_offline_drops_voice_too(self):
        # A hard direct address still must not ghost the user, AND when voice
        # synthesis is offline the schema additionally drops send_voice_note —
        # capabilities alter the action schema, not just prompt advisory text.
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._waha = MagicMock()
        bot._waha.kokoro_enabled = True
        bot._tts_available = False
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("hi"))
        agent.observe = AsyncMock()
        bot._agent = agent

        payload = {
            "event": "message",
            "payload": {
                "id": "dm2",
                "from": "123@c.us",
                "body": "hello",
                "type": "chat",
                "_data": {"notifyName": "Friend"},
            },
        }
        await bot._handle_message(payload)

        agent.chat.assert_awaited_once()
        assert agent.chat.call_args.kwargs.get("output_cls") is WahaNoSilentNoVoiceAction


class TestMediaRefetch:
    """When a webhook delivers hasMedia=true but no downloadable media URL,
    the bot must re-fetch the message with downloadMedia=true."""

    @pytest.mark.asyncio
    async def test_refetches_message_when_media_unresolved(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("nice pic"))
        agent.observe = AsyncMock()
        bot._agent = agent

        client = MagicMock()
        client.get_message = AsyncMock(
            return_value={
                "id": "img1",
                "from": "123@c.us",
                "body": "mira esta imagen",
                "type": "image",
                "hasMedia": True,
                "mimetype": "image/jpeg",
                "media": {
                    "url": "http://localhost:3000/api/files/default/img.jpg",
                    "mimetype": "image/jpeg",
                },
                "_data": {"notifyName": "Sender"},
            }
        )
        client.download_media = AsyncMock(return_value=b"\xff\xd8\xff\xe0fake-jpeg")
        bot._waha_client = client

        payload = {
            "event": "message",
            "payload": {
                "id": "img1",
                "from": "123@c.us",
                "body": "mira esta imagen",
                "type": "image",
                "hasMedia": True,
                "mimetype": "image/jpeg",
                "_data": {"notifyName": "Sender", "author": "123@c.us"},
            },
        }
        await bot._handle_message(payload)

        # The bot re-fetched the message to get the media URL.
        client.get_message.assert_awaited_once_with("123@c.us", "img1", download_media=True)
        # The image bytes were downloaded and passed to the agent.
        agent.chat.assert_awaited_once()
        images = agent.chat.call_args.kwargs.get("images")
        assert images is not None
        assert len(images) == 1
        assert images[0] == b"\xff\xd8\xff\xe0fake-jpeg"

    @pytest.mark.asyncio
    async def test_no_refetch_when_media_already_resolved(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("nice pic"))
        agent.observe = AsyncMock()
        bot._agent = agent

        client = MagicMock()
        client.get_message = AsyncMock()
        client.download_media = AsyncMock(return_value=b"img-bytes")
        bot._waha_client = client

        payload = {
            "event": "message",
            "payload": {
                "id": "img1",
                "from": "123@c.us",
                "body": "mira esta imagen",
                "type": "image",
                "hasMedia": True,
                "mimetype": "image/jpeg",
                "mediaUrl": "http://localhost:3000/api/files/default/img.jpg",
                "_data": {"notifyName": "Sender", "author": "123@c.us"},
            },
        }
        await bot._handle_message(payload)

        # Media was already resolvable from the webhook payload; no re-fetch.
        client.get_message.assert_not_awaited()


def _dm_payload(body: str, *, msg_id: str | None = None) -> dict:
    return {
        "event": "message",
        "payload": {
            "id": msg_id or body,
            "from": "123@c.us",
            "body": body,
            "type": "chat",
            "_data": {"notifyName": "Friend"},
        },
    }


class TestInstagramEnrichment:
    @pytest.mark.asyncio
    async def test_appends_image_and_tag(self, monkeypatch):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("nice shot"))
        agent.observe = AsyncMock()
        bot._agent = agent

        monkeypatch.setattr(
            "kai.bots.waha.fetch_instagram_post",
            lambda _code: ("data", [b"\x89PNG fake"]),
        )

        await bot._handle_message(
            _dm_payload("look https://www.instagram.com/p/DZ8w3urCS30/", msg_id="ig1")
        )

        agent.chat.assert_awaited_once()
        kwargs = agent.chat.call_args.kwargs
        images = kwargs.get("images")
        assert images is not None
        assert len(images) == 1
        assert images[0] == b"\x89PNG fake"
        sent_text = agent.chat.call_args.args[0]
        assert sent_text.startswith("[instagram post:\n  data]")

    @pytest.mark.asyncio
    async def test_fetch_failure_degrades_gracefully(self, monkeypatch):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("ok"))
        agent.observe = AsyncMock()
        bot._agent = agent

        def _boom(_code):
            raise RuntimeError("instagram down")

        monkeypatch.setattr("kai.bots.waha.fetch_instagram_post", _boom)

        body = "hey https://www.instagram.com/reel/ABC123_/"
        await bot._handle_message(_dm_payload(body, msg_id="ig2"))

        agent.chat.assert_awaited_once()
        kwargs = agent.chat.call_args.kwargs
        assert kwargs.get("images") in (None, [], b"")
        # The original text reaches the agent untouched (no IG tag prepended).
        sent_text = agent.chat.call_args.args[0]
        assert "[instagram post:" not in sent_text
        assert "hey https://www.instagram.com/reel/ABC123_/" in sent_text

    @pytest.mark.asyncio
    async def test_no_ig_url_does_not_fetch(self, monkeypatch):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("hi"))
        agent.observe = AsyncMock()
        bot._agent = agent

        called = False

        def _should_not_run(_code):
            nonlocal called
            called = True
            return ("data", [b"x"])

        monkeypatch.setattr("kai.bots.waha.fetch_instagram_post", _should_not_run)

        await bot._handle_message(_dm_payload("just chatting", msg_id="ig3"))

        assert called is False
        agent.chat.assert_awaited_once()
        assert agent.chat.call_args.kwargs.get("images") in (None, [])

    @pytest.mark.asyncio
    async def test_multi_image_carousel_appends_all(self, monkeypatch):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("cool carousel"))
        agent.observe = AsyncMock()
        bot._agent = agent

        imgs = [b"\x89PNG one", b"\x89PNG two", b"\x89PNG three"]
        monkeypatch.setattr(
            "kai.bots.waha.fetch_instagram_post",
            lambda _code: ("carousel data", imgs),
        )

        await bot._handle_message(
            _dm_payload("https://www.instagram.com/p/MULTI123/", msg_id="ig4")
        )

        agent.chat.assert_awaited_once()
        images = agent.chat.call_args.kwargs.get("images")
        assert images is not None
        assert images == imgs


class TestYouTubeEnrichment:
    @pytest.mark.asyncio
    async def test_appends_transcript_tag(self, monkeypatch):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("nice summary"))
        agent.observe = AsyncMock()
        bot._agent = agent

        monkeypatch.setattr(
            "kai.bots.waha.fetch_youtube_transcript",
            lambda vid: {
                "video_id": vid,
                "language": "English",
                "transcript_text": "hello world transcript",
                "url": f"https://www.youtube.com/watch?v={vid}",
            },
        )

        await bot._handle_message(
            _dm_payload("check this https://www.youtube.com/watch?v=G2IWYXxO324", msg_id="yt1")
        )

        agent.chat.assert_awaited_once()
        sent_text = agent.chat.call_args.args[0]
        assert sent_text.startswith("[youtube transcript:")
        assert "hello world transcript" in sent_text
        # No images for youtube.
        assert agent.chat.call_args.kwargs.get("images") in (None, [])

    @pytest.mark.asyncio
    async def test_fetch_failure_degrades_gracefully(self, monkeypatch):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("ok"))
        agent.observe = AsyncMock()
        bot._agent = agent

        def _boom(_vid):
            raise RuntimeError("youtube down")

        monkeypatch.setattr("kai.bots.waha.fetch_youtube_transcript", _boom)

        body = "hey https://youtu.be/9LLZBVTid4I"
        await bot._handle_message(_dm_payload(body, msg_id="yt2"))

        agent.chat.assert_awaited_once()
        sent_text = agent.chat.call_args.args[0]
        assert "[youtube transcript:" not in sent_text
        assert "hey https://youtu.be/9LLZBVTid4I" in sent_text

    @pytest.mark.asyncio
    async def test_no_yt_url_does_not_fetch(self, monkeypatch):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("hi"))
        agent.observe = AsyncMock()
        bot._agent = agent

        called = False

        def _should_not_run(_vid):
            nonlocal called
            called = True
            return {"transcript_text": "x"}

        monkeypatch.setattr("kai.bots.waha.fetch_youtube_transcript", _should_not_run)

        await bot._handle_message(_dm_payload("just chatting", msg_id="yt3"))

        assert called is False
        agent.chat.assert_awaited_once()
        sent_text = agent.chat.call_args.args[0]
        assert "[youtube transcript:" not in sent_text

    @pytest.mark.asyncio
    async def test_error_result_no_tag(self, monkeypatch):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=_chat_result("ok"))
        agent.observe = AsyncMock()
        bot._agent = agent

        monkeypatch.setattr(
            "kai.bots.waha.fetch_youtube_transcript",
            lambda vid: {"video_id": vid, "error": "No captions"},
        )

        body = "https://www.youtube.com/shorts/ABCDEFGHIJK"
        await bot._handle_message(_dm_payload(body, msg_id="yt4"))

        agent.chat.assert_awaited_once()
        sent_text = agent.chat.call_args.args[0]
        assert "[youtube transcript:" not in sent_text


class TestSeenStore:
    def test_load_round_trip(self, tmp_path):
        from kai.bots.waha.seen_store import SeenStore

        path = tmp_path / "seen.json"
        s = SeenStore(path, max_size=4)
        s.add("m1")
        s.add("m2")

        # A fresh instance loading the same file sees both IDs.
        s2 = SeenStore(path, max_size=4)
        assert s2.is_seen("m1")
        assert s2.is_seen("m2")
        assert not s2.is_seen("m3")

    def test_evicts_oldest_when_capped(self, tmp_path):
        from kai.bots.waha.seen_store import SeenStore

        path = tmp_path / "seen.json"
        s = SeenStore(path, max_size=2)
        s.add("m1")
        s.add("m2")
        s.add("m3")  # evicts m1 (oldest)

        assert not s.is_seen("m1")
        assert s.is_seen("m2")
        assert s.is_seen("m3")

        # And the persisted file reflects the eviction.
        s2 = SeenStore(path, max_size=2)
        assert not s2.is_seen("m1")
        assert s2.is_seen("m2")
        assert s2.is_seen("m3")

    def test_missing_file_starts_empty(self, tmp_path):
        from kai.bots.waha.seen_store import SeenStore

        s = SeenStore(tmp_path / "nope.json", max_size=4)
        assert not s.is_seen("anything")

    def test_corrupt_json_logs_and_starts_empty(self, tmp_path, caplog):
        from kai.bots.waha.seen_store import SeenStore

        path = tmp_path / "seen.json"
        path.write_text("{not valid json", encoding="utf-8")
        with caplog.at_level("WARNING"):
            s = SeenStore(path, max_size=4)
        assert not s.is_seen("m1")
        assert any("Failed to load seen IDs" in r.message for r in caplog.records)

    def test_none_path_is_in_memory_only(self):
        from kai.bots.waha.seen_store import SeenStore

        s = SeenStore(None, max_size=4)
        s.add("m1")
        assert s.is_seen("m1")
        # No file was ever written (path is None); a second instance can't see it.
        s2 = SeenStore(None, max_size=4)
        assert not s2.is_seen("m1")

    def test_re_adding_existing_id_is_noop_no_rewrite(self, tmp_path):
        from kai.bots.waha.seen_store import SeenStore

        path = tmp_path / "seen.json"
        s = SeenStore(path, max_size=4)
        s.add("m1")
        mtime1 = path.stat().st_mtime_ns
        # Re-adding must not trigger another write.
        s.add("m1")
        assert path.stat().st_mtime_ns == mtime1

    async def test_add_async_persists_offloaded(self, tmp_path):
        from kai.bots.waha.seen_store import SeenStore

        path = tmp_path / "seen.json"
        s = SeenStore(path, max_size=4)
        await s.add_async("m1")
        # In-memory update is immediate; the write was offloaded but awaited.
        assert s.is_seen("m1")
        s2 = SeenStore(path, max_size=4)
        assert s2.is_seen("m1")

    async def test_add_async_existing_id_is_noop(self, tmp_path):
        from kai.bots.waha.seen_store import SeenStore

        path = tmp_path / "seen.json"
        s = SeenStore(path, max_size=4)
        await s.add_async("m1")
        mtime1 = path.stat().st_mtime_ns
        await s.add_async("m1")
        assert path.stat().st_mtime_ns == mtime1

    async def test_add_async_none_path_in_memory_only(self):
        from kai.bots.waha.seen_store import SeenStore

        s = SeenStore(None, max_size=4)
        await s.add_async("m1")
        assert s.is_seen("m1")


class TestSeenMessagePersistence:
    """A seen ID on one Bot instance must be seen by a fresh instance."""

    async def test_seen_survives_new_bot_instance(self, tmp_path):
        from kai.bots.waha.seen_store import SeenStore

        path = tmp_path / "waha.seen.json"
        bot1 = _make_bot()
        bot1._seen_store = SeenStore(path, max_size=2048)
        # First sighting: not seen, gets recorded to disk.
        assert await bot1._is_seen_message("msg-abc") is False
        # Second call on the same instance: seen.
        assert await bot1._is_seen_message("msg-abc") is True

        # A brand-new Bot instance (simulating a restart) loading the same
        # store file must still see "msg-abc".
        bot2 = _make_bot()
        bot2._seen_store = SeenStore(path, max_size=2048)
        assert await bot2._is_seen_message("msg-abc") is True

    async def test_empty_message_id_never_seen(self):
        bot = _make_bot()
        assert await bot._is_seen_message("") is False


class TestSleepStore:
    def test_load_round_trip(self, tmp_path):
        path = tmp_path / "sleep.json"
        s = SleepStore(path)
        s.mark("chat1@g.us", True)
        s.mark("chat2@g.us", True)

        # A fresh instance loading the same file sees both asleep.
        s2 = SleepStore(path)
        assert s2.is_sleeping("chat1@g.us")
        assert s2.is_sleeping("chat2@g.us")
        assert not s2.is_sleeping("chat3@g.us")

    def test_set_false_clears_and_persists(self, tmp_path):
        path = tmp_path / "sleep.json"
        s = SleepStore(path)
        s.mark("chat1@g.us", True)
        s.mark("chat1@g.us", False)

        assert not s.is_sleeping("chat1@g.us")
        # And persisted as awake (absent from the file).
        s2 = SleepStore(path)
        assert not s2.is_sleeping("chat1@g.us")

    def test_missing_file_starts_empty(self, tmp_path):
        s = SleepStore(tmp_path / "nope.json")
        assert not s.is_sleeping("anything")

    def test_corrupt_json_logs_and_starts_empty(self, tmp_path, caplog):
        path = tmp_path / "sleep.json"
        path.write_text("{not valid json", encoding="utf-8")
        with caplog.at_level("WARNING"):
            s = SleepStore(path)
        assert not s.is_sleeping("chat1@g.us")
        assert any("Failed to load sleep state" in r.message for r in caplog.records)

    def test_none_path_is_in_memory_only(self):
        s = SleepStore(None)
        s.mark("chat1@g.us", True)
        assert s.is_sleeping("chat1@g.us")
        # A second in-memory instance can't see it (no file written).
        s2 = SleepStore(None)
        assert not s2.is_sleeping("chat1@g.us")

    def test_idempotent_set_is_noop_no_rewrite(self, tmp_path):
        path = tmp_path / "sleep.json"
        s = SleepStore(path)
        s.mark("chat1@g.us", True)
        mtime1 = path.stat().st_mtime_ns
        # Setting the same state again must not trigger another write.
        s.mark("chat1@g.us", True)
        assert path.stat().st_mtime_ns == mtime1
        # Nor clearing an already-awake chat.
        s.mark("chat2@g.us", False)
        assert path.stat().st_mtime_ns == mtime1

    def test_all_returns_copy(self):
        s = SleepStore(None)
        s.mark("a@g.us", True)
        s.mark("b@g.us", True)
        snapshot = s.all()
        assert snapshot == {"a@g.us", "b@g.us"}
        # Mutating the returned set must not affect the store.
        snapshot.add("c@g.us")
        assert not s.is_sleeping("c@g.us")


class TestSleepPersistence:
    """Sleep state on one Bot instance must be seen by a fresh instance."""

    def test_sleep_survives_new_bot_instance(self, tmp_path):
        path = tmp_path / "waha.sleep.json"
        bot1 = _make_bot()
        bot1._sleep_store = SleepStore(path)
        bot1._sleep_store.mark("group@g.us", True)

        # A brand-new Bot instance (simulating a restart) loading the same
        # store file must still see the chat as asleep.
        bot2 = _make_bot()
        bot2._sleep_store = SleepStore(path)
        assert bot2._sleep_store.is_sleeping("group@g.us") is True

    def test_wake_survives_new_bot_instance(self, tmp_path):
        path = tmp_path / "waha.sleep.json"
        bot1 = _make_bot()
        bot1._sleep_store = SleepStore(path)
        bot1._sleep_store.mark("group@g.us", True)
        bot1._sleep_store.mark("group@g.us", False)

        bot2 = _make_bot()
        bot2._sleep_store = SleepStore(path)
        assert bot2._sleep_store.is_sleeping("group@g.us") is False


class TestCapabilitiesState:
    """``_capabilities_state`` powers the ``/status`` route and must reflect the
    media config flags — including the new ``video`` capability."""

    def test_video_enabled_by_default(self):
        bot = _make_bot()
        bot._waha = None  # no TTS; isolates the media flags
        caps = bot._capabilities_state()
        assert "video" in caps
        assert caps["video"] is True
        assert caps["vision"] is True
        assert caps["instagram"] is True

    def test_video_disabled_when_flag_off(self):
        bot = _make_bot(BotConfig(media=MediaConfig(video_enabled=False)))
        bot._waha = None
        caps = bot._capabilities_state()
        assert caps["video"] is False
        # Other flags are independent.
        assert caps["vision"] is True

    def test_voice_and_tts_flags(self):
        bot = _make_bot()
        bot._waha = None
        bot._stt = None
        assert bot._capabilities_state()["voice_to_text"] is False
        # With no _waha, tts cannot be available.
        assert bot._capabilities_state()["text_to_voice"] is False
