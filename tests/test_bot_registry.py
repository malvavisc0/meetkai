import pytest

from kai.bots import list_bots, load_bot
from kai.cockpit.bots import BOT_TYPES


class TestListBots:
    def test_finds_waha_bot(self):
        assert "waha" in list_bots()

    def test_finds_email_bot(self):
        assert "email" in list_bots()


class TestLoadBot:
    def test_loads_waha_bot(self):
        bot = load_bot("waha")
        assert bot.name == "waha"
        assert bot.bot_dir.exists()

    def test_loads_email_bot(self):
        bot = load_bot("email")
        assert bot.name == "email"
        assert bot.bot_dir.exists()
        assert (bot.bot_dir / "prompt.md").is_file()
        assert (bot.bot_dir / "config.json").is_file()

    def test_raises_on_unknown_bot(self):
        with pytest.raises(ValueError, match="not found"):
            load_bot("nonexistent")


class TestEmailBotType:
    def test_required_connections(self):
        bt = BOT_TYPES["email"]
        assert bt.required_connections == ["resend", "smtp"]

    def test_supported_connections(self):
        bt = BOT_TYPES["email"]
        assert bt.supported_connections == ["database"]

    def test_feature_flags(self):
        bt = BOT_TYPES["email"]
        assert bt.feature_flags == ["image"]

    def test_required_settings(self):
        bt = BOT_TYPES["email"]
        assert "language" in bt.required_settings
