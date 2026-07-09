import pytest

from kai.bots import list_bots, load_bot


class TestListBots:
    def test_finds_waha_bot(self):
        assert "waha" in list_bots()


class TestLoadBot:
    def test_loads_waha_bot(self):
        bot = load_bot("waha")
        assert bot.name == "waha"
        assert bot.bot_dir.exists()

    def test_raises_on_unknown_bot(self):
        with pytest.raises(ValueError, match="not found"):
            load_bot("nonexistent")
