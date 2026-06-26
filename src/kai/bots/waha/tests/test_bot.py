import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kai.bots.waha import Bot, BotConfig, ParticipationConfig, _build_webhook_url
from kai.bots.waha.history import register_chat_history_tool


def _make_bot(config: BotConfig | None = None, bot_dir: Path | None = None) -> Bot:
    return Bot(bot_dir=bot_dir or Path("."), config=config or BotConfig())


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

    def test_group_reply_to_bot_without_keyword(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        assert (
            bot._should_respond(
                "no keyword here", is_group=True, mentions_bot=False, replies_to_bot=True
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
        assert bot._should_respond("Hey KAI", is_group=True, mentions_bot=False) is True

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
        bot._bot_ids.add("4917662716239@c.us")
        bot._learn_bot_identity("4917662716239@c.us", "154868568301592@lid")
        assert "154868568301592@lid" in bot._bot_ids
        # The learned LID makes a reply addressed by LID resolve to the bot.
        assert bot._is_reply_to_bot({"replyTo": {"participant": "154868568301592@lid"}}) is True

    def test_ignores_other_participants(self):
        bot = _make_bot()
        bot._bot_ids.add("4917662716239@c.us")
        bot._learn_bot_identity("99999@c.us", "88888@lid")
        assert "88888@lid" not in bot._bot_ids

    def test_noop_when_no_known_identity(self):
        bot = _make_bot()
        bot._learn_bot_identity("4917662716239@c.us", "154868568301592@lid")
        assert "154868568301592@lid" not in bot._bot_ids

    def test_handles_missing_lid(self):
        bot = _make_bot()
        bot._bot_ids.add("4917662716239@c.us")
        bot._learn_bot_identity("4917662716239@c.us", None)
        assert bot._bot_ids == {"4917662716239@c.us"}


class TestHasToolCallLeak:
    def test_detects_tool_call_block(self):
        from kai.bots.waha.processing import has_tool_call_leak

        leak = (
            "web_search\n```html\n<arg_key>query</arg_key>\n"
            "<arg_value>significado</arg_value>\n</tool_call>\n```"
        )
        assert has_tool_call_leak(leak) is True

    def test_detects_hyphenless_variant(self):
        from kai.bots.waha.processing import has_tool_call_leak

        assert has_tool_call_leak("<argkey>query</argkey>") is True

    def test_clean_reply_is_not_a_leak(self):
        from kai.bots.waha.processing import has_tool_call_leak

        assert has_tool_call_leak("sure, that movie comes out friday") is False

    def test_empty_is_not_a_leak(self):
        from kai.bots.waha.processing import has_tool_call_leak

        assert has_tool_call_leak("") is False


class TestPostProcess:
    def test_strips_bold(self):
        assert _make_bot()._post_process("**hello**") == "hello"

    def test_strips_italic(self):
        assert _make_bot()._post_process("*hello*") == "hello"

    def test_strips_underscore(self):
        assert _make_bot()._post_process("_hello_") == "hello"

    def test_strips_bullet_points(self):
        assert _make_bot()._post_process("- item one\n- item two") == "item one\nitem two"

    def test_strips_hashtags(self):
        assert _make_bot()._post_process("hello #world #tag") == "hello"

    def test_keeps_single_emoji(self):
        assert _make_bot()._post_process("hello 😭") == "hello 😭"

    def test_keeps_single_variation_selector_emoji(self):
        # "❤️" is U+2764 + U+FE0F: one emoji, two codepoints. It must not be
        # mistaken for two emojis nor truncated to a bare "❤".
        assert _make_bot()._post_process("ok ❤️") == "ok ❤️"

    def test_keeps_single_zwj_emoji(self):
        # ZWJ family emoji is a single grapheme cluster.
        family = "👨\u200d👩\u200d👧"
        assert _make_bot()._post_process(f"nice {family}") == f"nice {family}"

    def test_strips_extra_emojis(self):
        result = _make_bot()._post_process("hi 😭😂🔥")
        assert result.count("😭") == 1
        assert "😂" not in result
        assert "🔥" not in result

    def test_no_emoji_preserved(self):
        assert _make_bot()._post_process("hello world") == "hello world"

    def test_combined_markdown_and_emojis(self):
        result = _make_bot()._post_process("**hello** 😭😂 world")
        assert "**" not in result
        assert result.count("😭") == 1
        assert "😂" not in result

    def test_strips_multiple_hashtags(self):
        assert _make_bot()._post_process("#one #two #three done") == "done"

    def test_empty_string(self):
        assert _make_bot()._post_process("") == ""

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

    def test_keeps_period_on_long_reply(self):
        long_reply = (
            "Esta es una respuesta suficientemente larga como para conservar su punto final."
        )
        assert _make_bot()._post_process(long_reply) == long_reply

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

    def test_strips_backticks_around_silent_marker(self):
        # A backtick-wrapped <<silent>> reaches _post_process only when silence
        # detection already failed; still strip so nothing leaks if it does.
        assert _make_bot()._post_process("`<<silent>>`") == "<<silent>>"


class TestLoadConfig:
    def test_parses_config_json(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"trigger_keyword": "kai", "language": "Spanish"}')
        bot = _make_bot(bot_dir=tmp_path)
        config = bot._load_config(config_path=config_file)
        assert config.trigger_keyword == "kai"
        assert config.language == "Spanish"

    def test_parses_timezone(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"trigger_keyword": "kai", "timezone": "America/Santo_Domingo"}')
        bot = _make_bot(bot_dir=tmp_path)
        config = bot._load_config(config_path=config_file)
        assert config.timezone == "America/Santo_Domingo"

    def test_timezone_defaults_none(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"trigger_keyword": "kai"}')
        bot = _make_bot(bot_dir=tmp_path)
        config = bot._load_config(config_path=config_file)
        assert config.timezone is None

    def test_empty_timezone_treated_as_none(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"trigger_keyword": "kai", "timezone": "  "}')
        bot = _make_bot(bot_dir=tmp_path)
        config = bot._load_config(config_path=config_file)
        assert config.timezone is None

    def test_missing_config_file_raises(self, tmp_path):
        missing = tmp_path / "config.json"
        bot = _make_bot(bot_dir=tmp_path)
        with pytest.raises(FileNotFoundError):
            bot._load_config(config_path=missing)

    def test_whitelist_string_treated_as_empty(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"whitelist": "123@c.us"}')
        bot = _make_bot(bot_dir=tmp_path)
        config = bot._load_config(config_path=config_file)
        assert config.whitelist == []

    def test_blacklist_string_treated_as_empty(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"blacklist": "555@g.us"}')
        bot = _make_bot(bot_dir=tmp_path)
        config = bot._load_config(config_path=config_file)
        assert config.blacklist == []

    def test_whitelist_invalid_entries_skipped(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"whitelist": ["123@c.us", 42, "", "456@g.us"]}')
        bot = _make_bot(bot_dir=tmp_path)
        config = bot._load_config(config_path=config_file)
        assert config.whitelist == ["123@c.us", "456@g.us"]

    def test_media_config_defaults(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"trigger_keyword": "kai"}')
        bot = _make_bot(bot_dir=tmp_path)
        config = bot._load_config(config_path=config_file)
        assert config.media.image_enabled is True
        assert config.media.voice_enabled is True
        assert config.media.max_size_mb == 10

    def test_media_config_custom(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(
            '{"trigger_keyword": "kai", "media": '
            '{"image_enabled": false, "voice_enabled": true, "max_size_mb": 5}}'
        )
        bot = _make_bot(bot_dir=tmp_path)
        config = bot._load_config(config_path=config_file)
        assert config.media.image_enabled is False
        assert config.media.voice_enabled is True
        assert config.media.max_size_mb == 5


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
            lambda: Settings(_env_file=None, configs_dir=configs_dir),
        )
        bot = _make_bot(bot_dir=tmp_path)
        path = bot.resolve_config_path()
        assert path == external

    def test_falls_back_to_packaged_default(self, tmp_path, monkeypatch):
        from kai.config.settings import Settings

        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        packaged = tmp_path / "config.json"
        packaged.write_text('{"trigger_keyword": "kai"}')

        monkeypatch.setattr(
            "kai.bots.base.get_settings",
            lambda: Settings(_env_file=None, configs_dir=configs_dir),
        )
        bot = _make_bot(bot_dir=tmp_path)
        path = bot.resolve_config_path()
        assert path == packaged

    def test_returns_none_when_no_config_exists(self, tmp_path, monkeypatch):
        from kai.config.settings import Settings

        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        monkeypatch.setattr(
            "kai.bots.base.get_settings",
            lambda: Settings(_env_file=None, configs_dir=configs_dir),
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
            lambda: Settings(_env_file=None, configs_dir=configs_dir),
        )
        bot = _make_bot(bot_dir=tmp_path)
        config = bot._load_config()
        assert config.whitelist == ["group@g.us"]
        assert config.language == "Spanish"


def _make_chat_history_bot():
    """Create a bot with a registered get_chat_history tool, return (bot, tool)."""
    bot = _make_bot(BotConfig(trigger_keyword="kai"))
    from kai.agent.context import ToolContext

    bot._tool_context = ToolContext(chat_id="group@g.us")
    bot._waha_client = MagicMock()
    bot._waha_client.get_chat_messages = AsyncMock()

    captured: list = []

    class FakeAgent:
        def register_tool(self, tool):
            captured.append(tool)

    register_chat_history_tool(FakeAgent(), bot=bot)
    return bot, captured[0]


async def _call_tool(tool, **kwargs):
    """Invoke a FunctionTool async and return its string content."""
    output = await tool.acall(**kwargs)
    return str(output.content)


class TestGetChatHistory:
    @pytest.mark.asyncio
    async def test_formats_messages_chronological(self):
        bot, tool = _make_chat_history_bot()
        # WAHA returns newest-first; tool reverses to oldest-first.
        bot._waha_client.get_chat_messages.return_value = [
            {"id": "m2", "body": "second", "fromMe": False, "participant": "12345678902@c.us"},
            {"id": "m1", "body": "first", "fromMe": False, "participant": "12345678901@c.us"},
        ]
        result = await _call_tool(tool, limit=50)
        lines = result.split("\n")
        assert lines[0] == "[12345678901] first"
        assert lines[1] == "[12345678902] second"

    @pytest.mark.asyncio
    async def test_labels_bot_messages_as_kai(self):
        bot, tool = _make_chat_history_bot()
        bot._waha_client.get_chat_messages.return_value = [
            {"id": "m1", "body": "my reply", "fromMe": True},
            {"id": "m0", "body": "hello", "fromMe": False, "participant": "12345678901@c.us"},
        ]
        result = await _call_tool(tool)
        assert "[Kai] my reply" in result
        assert "[12345678901] hello" in result

    @pytest.mark.asyncio
    async def test_resolves_sender_from_roster(self):
        bot, tool = _make_chat_history_bot()
        bot._rosters["group@g.us"] = {"12345678901@c.us": "Juan"}
        bot._waha_client.get_chat_messages.return_value = [
            {"id": "m1", "body": "hola", "fromMe": False, "participant": "12345678901@c.us"},
        ]
        result = await _call_tool(tool)
        assert result == "[Juan] hola"

    @pytest.mark.asyncio
    async def test_falls_back_to_notify_name(self):
        bot, tool = _make_chat_history_bot()
        bot._waha_client.get_chat_messages.return_value = [
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
        bot, tool = _make_chat_history_bot()
        # Some WAHA versions put notifyName at the top level, not in _data.
        bot._waha_client.get_chat_messages.return_value = [
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
        bot, tool = _make_chat_history_bot()
        # When participant is absent, _data.author (dict form) is the sender.
        bot._waha_client.get_chat_messages.return_value = [
            {
                "id": "m1",
                "body": "hello",
                "fromMe": False,
                "from": "group@g.us",
                "_data": {
                    "author": {"_serialized": "12345678901@c.us"},
                    "notifyName": "Francisco",
                },
            },
        ]
        result = await _call_tool(tool)
        assert result == "[Francisco] hello"

    @pytest.mark.asyncio
    async def test_falls_back_to_data_author_string(self):
        bot, tool = _make_chat_history_bot()
        bot._waha_client.get_chat_messages.return_value = [
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
        bot, tool = _make_chat_history_bot()
        # In a DM, participant is absent and 'from' is the other person's JID.
        bot._waha_client.get_chat_messages.return_value = [
            {"id": "m1", "body": "hey", "fromMe": False, "from": "12345678901@c.us"},
        ]
        result = await _call_tool(tool)
        assert result == "[12345678901] hey"

    @pytest.mark.asyncio
    async def test_falls_back_to_phone_digits(self):
        bot, tool = _make_chat_history_bot()
        bot._waha_client.get_chat_messages.return_value = [
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
        bot, tool = _make_chat_history_bot()
        bot._waha_client.get_chat_messages.return_value = [
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
        bot, tool = _make_chat_history_bot()
        bot._waha_client.get_chat_messages.return_value = [
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
        bot, tool = _make_chat_history_bot()
        bot._waha_client.get_chat_messages.return_value = [
            {"id": "m1", "body": "real msg", "fromMe": False, "participant": "12345678901@c.us"},
            {"id": "m0", "body": "", "fromMe": False, "participant": "12345678902@c.us"},
            {"id": "m0b", "body": "   ", "fromMe": False, "participant": "12345678903@c.us"},
        ]
        result = await _call_tool(tool)
        assert result == "[12345678901] real msg"

    @pytest.mark.asyncio
    async def test_returns_message_when_no_messages_found(self):
        bot, tool = _make_chat_history_bot()
        bot._waha_client.get_chat_messages.return_value = []
        result = await _call_tool(tool)
        assert result == "No text messages found."

    @pytest.mark.asyncio
    async def test_clamps_limit_and_offset(self):
        bot, tool = _make_chat_history_bot()
        bot._waha_client.get_chat_messages.return_value = []
        await _call_tool(tool, limit=500, offset=-10)
        call_kwargs = bot._waha_client.get_chat_messages.call_args.kwargs
        assert call_kwargs["limit"] == 200
        assert call_kwargs["offset"] == 0

    @pytest.mark.asyncio
    async def test_passes_limit_and_offset_to_client(self):
        bot, tool = _make_chat_history_bot()
        bot._waha_client.get_chat_messages.return_value = []
        await _call_tool(tool, limit=100, offset=50)
        call_kwargs = bot._waha_client.get_chat_messages.call_args.kwargs
        assert call_kwargs["limit"] == 100
        assert call_kwargs["offset"] == 50

    @pytest.mark.asyncio
    async def test_returns_error_string_on_client_exception(self):
        bot, tool = _make_chat_history_bot()
        bot._waha_client.get_chat_messages.side_effect = RuntimeError("WAHA down")
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

        register_chat_history_tool(FakeAgent(), bot=bot)
        result = await _call_tool(captured[0])
        assert result == "Error: no chat context available"

    @pytest.mark.asyncio
    async def test_creates_and_closes_client_when_none(self, monkeypatch):
        bot, tool = _make_chat_history_bot()
        # Simulate no persistent client; a temporary one must be created & closed.
        bot._waha_client = None
        bot._waha = MagicMock()
        mock_client = MagicMock()
        mock_client.get_chat_messages = AsyncMock(return_value=[])
        mock_client.close = AsyncMock()
        monkeypatch.setattr("kai.bots.waha.client.WahaClient", lambda *a, **kw: mock_client)
        result = await _call_tool(tool)
        assert result == "No text messages found."
        mock_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_close_persistent_client_on_success(self):
        bot, tool = _make_chat_history_bot()
        bot._waha_client.get_chat_messages.return_value = [
            {"id": "m1", "body": "msg", "fromMe": False, "participant": "12345678901@c.us"},
        ]
        await _call_tool(tool)
        bot._waha_client.close.assert_not_called()


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
    async def test_send_order_serialized_per_chat(self):
        """Two rapid messages in one chat must be processed in arrival order."""
        from unittest.mock import AsyncMock, MagicMock

        bot = _make_bot(BotConfig(trigger_keyword=""))
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
            return "reply"

        agent.chat = AsyncMock(side_effect=chat_stub)
        agent.observe = AsyncMock()
        bot._agent = agent
        bot._bot_ids.add("bot@c.us")
        bot._config.mentions_enabled = False
        bot._send_with_retry = AsyncMock()

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
    """Reasoning-model "channel" tokens must never leak into a sent message."""

    @pytest.mark.asyncio
    async def test_channels_plus_silent_treated_as_silent(self, monkeypatch):
        bot = _make_bot(
            BotConfig(
                trigger_keyword="kai",
                participation=ParticipationConfig(
                    enabled=True, rate=1.0, cooldown_seconds=0.0, streak_max=5
                ),
            )
        )
        bot._send_with_retry = AsyncMock()
        leaked = "<|channel>thought\n<channel|><|channel>thought\n<channel|><<silent>>"
        agent = MagicMock()
        agent.chat = AsyncMock(return_value=leaked)
        agent.observe = AsyncMock()
        bot._agent = agent
        monkeypatch.setattr("random.random", lambda: 0.9)

        await bot._handle_message(_group_payload("just chatting"))

        # Silent: nothing sent, no <<silent>> reaches the chat.
        bot._send_with_retry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_channels_stripped_from_real_reply(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._send_with_retry = AsyncMock()
        agent = MagicMock()
        agent.chat = AsyncMock(
            return_value="<|channel>thought\nreasoning here<channel|>hey what's up"
        )
        agent.observe = AsyncMock()
        bot._agent = agent

        await bot._handle_message(_group_payload("hey kai"))

        sent_text = bot._send_with_retry.await_args.args[1]
        assert "channel" not in sent_text
        assert "<<silent>>" not in sent_text
        assert sent_text == "hey what's up"


class TestSleepToken:
    """The <<sleep>> token drives the sleep state — the model decides, not regex."""

    @pytest.mark.asyncio
    async def test_sleep_token_sets_sleeping_and_sends_reply(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._send_with_retry = AsyncMock()
        agent = MagicMock()
        agent.chat = AsyncMock(return_value="ok, heading off <<sleep>>")
        agent.observe = AsyncMock()
        bot._agent = agent

        await bot._handle_message(_group_payload("kai go to sleep"))

        assert bot._sleeping.get("group@g.us") is True
        bot._send_with_retry.assert_awaited_once()
        sent = bot._send_with_retry.call_args.args[1]
        assert "<<sleep>>" not in sent
        assert "heading off" in sent

    @pytest.mark.asyncio
    async def test_sleep_token_with_no_text_uses_default_ack(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._send_with_retry = AsyncMock()
        agent = MagicMock()
        agent.chat = AsyncMock(return_value="<<sleep>>")
        agent.observe = AsyncMock()
        bot._agent = agent

        await bot._handle_message(_group_payload("kai go to sleep"))

        assert bot._sleeping.get("group@g.us") is True
        sent = bot._send_with_retry.call_args.args[1]
        assert sent == "going quiet, ping me if you need me"

    @pytest.mark.asyncio
    async def test_sleeping_bot_observes_non_addressed_without_llm(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._sleeping["group@g.us"] = True
        agent = MagicMock()
        agent.chat = AsyncMock(return_value="should not happen")
        agent.observe = AsyncMock()
        bot._agent = agent

        # Plain group message, nobody mentions or replies to the bot.
        await bot._handle_message(_group_payload("just chatting"))

        agent.chat.assert_not_awaited()
        agent.observe.assert_awaited()
        assert bot._sleeping.get("group@g.us") is True

    @pytest.mark.asyncio
    async def test_sleeping_bot_wakes_on_mention_with_real_reply(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._sleeping["group@g.us"] = True
        bot._send_with_retry = AsyncMock()
        bot._bot_ids.add("bot@c.us")
        agent = MagicMock()
        agent.chat = AsyncMock(return_value="hey, I'm back")
        agent.observe = AsyncMock()
        bot._agent = agent

        payload = _group_payload("@bot you there?", msg_id="m1")
        payload["payload"]["_data"]["mentionedJidList"] = ["bot@c.us"]
        await bot._handle_message(payload)

        assert bot._sleeping.get("group@g.us") is False
        agent.chat.assert_awaited_once()
        sent = bot._send_with_retry.call_args.args[1]
        assert sent == "hey, I'm back"

    @pytest.mark.asyncio
    async def test_sleeping_bot_stays_asleep_on_silent_reply(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._sleeping["group@g.us"] = True
        bot._bot_ids.add("bot@c.us")
        bot._send_with_retry = AsyncMock()
        agent = MagicMock()
        agent.chat = AsyncMock(return_value="<<silent>>")
        agent.observe = AsyncMock()
        bot._agent = agent

        payload = _group_payload("@bot ping", msg_id="m1")
        payload["payload"]["_data"]["mentionedJidList"] = ["bot@c.us"]
        await bot._handle_message(payload)

        assert bot._sleeping.get("group@g.us") is True
        bot._send_with_retry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sleeping_dm_wakes_bot(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._sleeping["123@c.us"] = True
        bot._send_with_retry = AsyncMock()
        agent = MagicMock()
        agent.chat = AsyncMock(return_value="hi again")
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

        assert bot._sleeping.get("123@c.us") is False
        bot._send_with_retry.assert_awaited_once()


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
                    enabled=True, rate=1.0, cooldown_seconds=60.0, streak_max=5
                ),
            )
        )
        bot._last_reply_at["g@g.us"] = 0.0  # monotonic, far in the past-ish
        # Force a recent reply.
        import time as _time

        bot._last_reply_at["g@g.us"] = _time.monotonic()
        assert bot._should_organically_participate("g@g.us", "anything", is_group=True) is False

    def test_streak_max_blocks_offer(self):
        bot = _make_bot(
            BotConfig(
                trigger_keyword="kai",
                participation=ParticipationConfig(
                    enabled=True, rate=1.0, cooldown_seconds=0.0, streak_max=2
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
                    enabled=True, rate=1.0, cooldown_seconds=100.0, streak_max=5
                ),
            )
        )
        import time as _time

        bot._last_reply_at["g@g.us"] = _time.monotonic() - 50.0
        # streak stays 0 (last turn was a skip / never replied)
        assert bot._should_organically_participate("g@g.us", "anything", is_group=True) is False

    def test_active_exchange_relaxes_cooldown(self):
        # Bot replied last (streak 1); a follow-up past the relaxed window but
        # still inside the normal cooldown is no longer blocked.
        bot = _make_bot(
            BotConfig(
                trigger_keyword="kai",
                participation=ParticipationConfig(
                    enabled=True, rate=1.0, cooldown_seconds=100.0, streak_max=5
                ),
            )
        )
        import time as _time

        bot._consecutive_replies["g@g.us"] = 1
        bot._last_reply_at["g@g.us"] = _time.monotonic() - 50.0  # 30 < 50 < 100
        assert bot._should_organically_participate("g@g.us", "anything", is_group=True) is True

    def test_active_exchange_still_blocks_within_relaxed_cooldown(self):
        # Even in an active exchange, a too-fast follow-up is throttled.
        bot = _make_bot(
            BotConfig(
                trigger_keyword="kai",
                participation=ParticipationConfig(
                    enabled=True, rate=1.0, cooldown_seconds=100.0, streak_max=5
                ),
            )
        )
        import time as _time

        bot._consecutive_replies["g@g.us"] = 1
        bot._last_reply_at["g@g.us"] = _time.monotonic() - 10.0  # 10 < 30 relaxed
        assert bot._should_organically_participate("g@g.us", "anything", is_group=True) is False

    def test_active_exchange_rate_boost_applies(self, monkeypatch):
        # With a zero base rate, a quick follow-up in an active exchange still
        # gets a chance thanks to the boost.
        bot = _make_bot(
            BotConfig(
                trigger_keyword="kai",
                participation=ParticipationConfig(
                    enabled=True, rate=0.0, cooldown_seconds=100.0, streak_max=5
                ),
            )
        )
        import time as _time

        bot._consecutive_replies["g@g.us"] = 1
        bot._last_reply_at["g@g.us"] = _time.monotonic() - 50.0  # within normal window
        monkeypatch.setattr("random.random", lambda: 0.3)
        assert bot._should_organically_participate("g@g.us", "anything", is_group=True) is True

    def test_no_rate_boost_after_long_gap(self, monkeypatch):
        # After the normal cooldown window has fully elapsed, the boost no
        # longer applies — only the base rate matters.
        bot = _make_bot(
            BotConfig(
                trigger_keyword="kai",
                participation=ParticipationConfig(
                    enabled=True, rate=0.0, cooldown_seconds=100.0, streak_max=5
                ),
            )
        )
        import time as _time

        bot._consecutive_replies["g@g.us"] = 1
        bot._last_reply_at["g@g.us"] = _time.monotonic() - 200.0  # past normal cooldown
        monkeypatch.setattr("random.random", lambda: 0.3)
        assert bot._should_organically_participate("g@g.us", "anything", is_group=True) is False

    @pytest.mark.asyncio
    async def test_non_summoned_group_message_offers_when_rate_one(self, monkeypatch):
        bot = _make_bot(
            BotConfig(
                trigger_keyword="kai",
                participation=ParticipationConfig(
                    enabled=True, rate=1.0, cooldown_seconds=0.0, streak_max=5
                ),
            )
        )
        agent = MagicMock()
        agent.chat = AsyncMock(return_value="<<silent>>")
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
                    enabled=True, rate=0.0, cooldown_seconds=0.0, streak_max=5
                ),
            )
        )
        agent = MagicMock()
        agent.chat = AsyncMock(return_value="nope")
        agent.observe = AsyncMock()
        bot._agent = agent
        monkeypatch.setattr("random.random", lambda: 0.5)

        await bot._handle_message(_group_payload("just chatting about pizza"))

        agent.chat.assert_not_awaited()
        agent.observe.assert_awaited()


class TestGroupRosterRefresh:
    @pytest.mark.asyncio
    async def test_group_message_triggers_roster_refresh(self, monkeypatch):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        agent = MagicMock()
        agent.chat = AsyncMock(return_value="<<silent>>")
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
        agent.chat = AsyncMock(return_value="hi")
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
        roster: dict[str, str] = {"72013750239365@lid": "Juan Palotes"}
        client = MagicMock()
        client.get_chat_participants = AsyncMock(
            return_value=[
                {"id": "4917662716239@c.us", "pn": "4917662716239@c.us", "role": "admin"},
                {"id": "18096184445@c.us", "pn": "18096184445@c.us", "role": "participant"},
            ]
        )
        bot._waha_client = client

        await bot._refresh_group_roster("g@g.us", roster)

        assert roster == {"72013750239365@lid": "Juan Palotes"}
        assert bot._group_admins["g@g.us"] == {"4917662716239@c.us"}

    @pytest.mark.asyncio
    async def test_refresh_respects_ttl(self, monkeypatch):
        import time as _time

        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        bot._roster_refreshed_at["g@g.us"] = _time.monotonic()  # just refreshed
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
        assert "People in this chat: Alice, Bob" in prompt
        assert "Admins: Alice" in prompt

    def test_omits_admins_line_when_none(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        roster = {"111@c.us": "Alice"}
        prompt = bot._build_per_chat_prompt("g@g.us", is_group=True, roster=roster)
        assert "Admins" not in prompt


class TestDMNoSilence:
    @pytest.mark.asyncio
    async def test_dm_forces_allow_silence_false(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        agent = MagicMock()
        agent.chat = AsyncMock(return_value="hi")
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
        assert agent.chat.call_args.kwargs.get("allow_silence") is False


class TestMediaRefetch:
    """When a webhook delivers hasMedia=true but no downloadable media URL,
    the bot must re-fetch the message with downloadMedia=true."""

    @pytest.mark.asyncio
    async def test_refetches_message_when_media_unresolved(self):
        bot = _make_bot(BotConfig(trigger_keyword="kai"))
        agent = MagicMock()
        agent.chat = AsyncMock(return_value="nice pic")
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
        agent.chat = AsyncMock(return_value="nice pic")
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
