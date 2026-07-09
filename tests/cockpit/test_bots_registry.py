"""Tests for the BotType registry (kai.cockpit.bots)."""

from kai.cockpit.bots import BOT_TYPES, LANGUAGE_VOICE_MAP, auto_pick_voice


class TestBotTypesRegistry:
    def test_waha_registered(self):
        assert "waha" in BOT_TYPES

    def test_waha_feature_flags(self):
        bt = BOT_TYPES["waha"]
        assert set(bt.feature_flags) == {"image", "stt", "tts", "video"}

    def test_waha_required_settings(self):
        bt = BOT_TYPES["waha"]
        assert "language" in bt.required_settings


class TestAutoPickVoice:
    def test_known_language(self):
        assert auto_pick_voice("Spanish") == LANGUAGE_VOICE_MAP["Spanish"]

    def test_unknown_language_falls_back(self):
        assert auto_pick_voice("Klingon") == "af_heart"
