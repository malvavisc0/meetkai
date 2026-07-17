"""Tests for the BotType registry (kai.cockpit.bots)."""

from kai.cockpit.bots import BOT_TYPES, auto_pick_voice


class TestBotTypesRegistry:
    def test_waha_registered(self):
        assert "waha" in BOT_TYPES


class TestAutoPickVoice:
    def test_unknown_language_falls_back(self):
        assert auto_pick_voice("Klingon") == "af_heart"
