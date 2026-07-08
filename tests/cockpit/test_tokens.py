"""Tests for the LoginRequest/LoginToken repository (kai.cockpit.tokens)."""

from datetime import UTC, datetime, timedelta

from kai.cockpit import tokens
from kai.cockpit.models import LoginRequest, LoginToken


class TestLoginRequestRepo:
    def test_create_login_request(self, db, user):
        req = tokens.create_login_request(db, user.id)
        assert req is not None
        assert req.status == "pending"
        assert req.user_id == user.id

    def test_duplicate_pending_request_prevented(self, db, user):
        first = tokens.create_login_request(db, user.id)
        assert first is not None
        second = tokens.create_login_request(db, user.id)
        assert second is None
        assert db.query(LoginRequest).filter(LoginRequest.user_id == user.id).count() == 1

    def test_get_pending_request(self, db, user):
        assert tokens.get_pending_request(db, user.id) is None
        req = tokens.create_login_request(db, user.id)
        assert req is not None
        fetched = tokens.get_pending_request(db, user.id)
        assert fetched is not None
        assert fetched.id == req.id

    def test_fulfill_request(self, db, user):
        req = tokens.create_login_request(db, user.id)
        assert req is not None
        tokens.fulfill_request(db, req.id, "tok-123")
        db.refresh(req)
        assert req.status == "fulfilled"
        assert req.token_id == "tok-123"
        assert req.fulfilled_at is not None
        assert tokens.get_pending_request(db, user.id) is None


class TestLoginTokenRepo:
    def test_create_login_token(self, db, user):
        token = tokens.create_login_token(db, user.id)
        assert token.user_id == user.id
        assert token.consumed_at is None
        created = datetime.fromisoformat(token.created_at)
        expires = datetime.fromisoformat(token.expires_at)
        assert (expires - created) == timedelta(minutes=tokens.TOKEN_TTL_MINUTES)

    def test_consume_login_token_happy_path(self, db, user):
        token = tokens.create_login_token(db, user.id)
        result = tokens.consume_login_token(db, token.token)
        assert result == user.id
        db.refresh(token)
        assert token.consumed_at is not None

    def test_consume_unknown_token_rejected(self, db, user):
        assert tokens.consume_login_token(db, "does-not-exist") is None

    def test_consume_already_consumed_rejected(self, db, user):
        token = tokens.create_login_token(db, user.id)
        assert tokens.consume_login_token(db, token.token) == user.id
        assert tokens.consume_login_token(db, token.token) is None

    def test_consume_expired_rejected(self, db, user):
        expired = LoginToken(
            token="expired-tok",
            user_id=user.id,
            created_at=(datetime.now(UTC) - timedelta(minutes=20)).isoformat(),
            expires_at=(datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
        )
        db.add(expired)
        db.commit()
        assert tokens.consume_login_token(db, "expired-tok") is None

    def test_cleanup_expired_tokens(self, db, user):
        expired = LoginToken(
            token="expired-tok",
            user_id=user.id,
            created_at=(datetime.now(UTC) - timedelta(minutes=20)).isoformat(),
            expires_at=(datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
        )
        db.add(expired)
        db.commit()
        deleted = tokens.cleanup_expired_tokens(db)
        assert deleted == 1
        assert db.query(LoginToken).count() == 0
