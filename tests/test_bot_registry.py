import pytest

from kai.bots import list_bots, load_bot


class TestListBots:
    def test_finds_email_bot(self):
        assert "email" in list_bots()


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
