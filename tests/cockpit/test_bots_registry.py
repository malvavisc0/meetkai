"""Tests for the BotType registry (kai.cockpit.bots)."""

from kai.cockpit.bots import BOT_TYPES


class TestBotTypesRegistry:
    def test_email_registered(self):
        assert "email" in BOT_TYPES
