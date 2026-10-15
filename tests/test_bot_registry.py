import pytest

from kai.bots import list_bots, load_bot
from kai.cockpit.bots import BOT_TYPES


class TestListBots:
    def test_finds_email_bot(self):
        assert "email" in list_bots()

    def test_does_not_find_waha_bot(self):
        assert "waha" not in list_bots()


class TestLoadBot:
    def test_loads_email_bot(self):
        bot = load_bot("email")
        assert bot.name == "email"
        assert bot.bot_dir.exists()
        assert (bot.bot_dir / "prompt.md").is_file()

    def test_raises_on_unknown_bot(self):
        with pytest.raises(ValueError, match="not found"):
            load_bot("nonexistent")

    def test_load_waha_raises(self):
        with pytest.raises(ValueError, match="not found"):
            load_bot("waha")


class TestEmailBotType:
    def test_required_connections(self):
        bt = BOT_TYPES["email"]
        assert bt.required_connections == ["resend", "smtp"]

    def test_supported_connections(self):
        bt = BOT_TYPES["email"]
        assert bt.supported_connections == ["database", "calcom"]

    def test_feature_flags(self):
        bt = BOT_TYPES["email"]
        assert bt.feature_flags == ["image"]

    def test_required_settings(self):
        bt = BOT_TYPES["email"]
        assert "language" in bt.required_settings
