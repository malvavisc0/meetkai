"""Tests for the BotType registry (kai.cockpit.bots)."""

import pytest

from kai.cockpit.bots import BOT_TYPES, auto_pick_voice


class TestBotTypesRegistry:
    def test_waha_registered(self):
        assert "waha" in BOT_TYPES


class TestAutoPickVoice:
    def test_known_language_picks_default_voice(self):
        assert auto_pick_voice("English") == "af_heart"

    def test_unknown_language_raises(self):
        with pytest.raises(ValueError, match="unsupported language"):
            auto_pick_voice("Klingon")
