"""Tests for MagicLinkProvider (kai.cockpit.auth_backends)."""

import pytest

from kai.cockpit import tokens
from kai.cockpit.auth_backends import MagicLinkProvider


class TestMagicLinkProvider:
    def test_initiate_login_requires_pending_request(self, db, user):
        provider = MagicLinkProvider(db)
        with pytest.raises(ValueError):
            provider.initiate_login(user.id)

    def test_initiate_login_happy_path(self, db, user):
        tokens.create_login_request(db, user.id)
        provider = MagicLinkProvider(db)
        token = provider.initiate_login(user.id)
        assert token.user_id == user.id
        # The pending request is now fulfilled.
        assert tokens.get_pending_request(db, user.id) is None

    def test_consume_login_single_use(self, db, user):
        tokens.create_login_request(db, user.id)
        provider = MagicLinkProvider(db)
        token = provider.initiate_login(user.id)
        assert provider.consume_login(token.token) == user.id
        assert provider.consume_login(token.token) is None

    def test_consume_login_unknown_token(self, db, user):
        provider = MagicLinkProvider(db)
        assert provider.consume_login("nonexistent") is None
