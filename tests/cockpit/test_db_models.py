"""Model-level tests: defaults, constraints, JSON round-tripping."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from kai.cockpit.models import Connection, Deployment, LoginRequest, LoginToken, User


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TestUser:
    def test_defaults(self, db):
        u = User(
            email="a@x.com",
            language="English",
            timezone="UTC",
            hmac_key="k",
            created_at=_now(),
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        assert u.id is not None
        assert u.is_disabled is False

    def test_email_unique(self, db):
        db.add(
            User(
                email="dup@x.com",
                language="English",
                timezone="UTC",
                hmac_key="k1",
                created_at=_now(),
            )
        )
        db.commit()
        db.add(
            User(
                email="dup@x.com",
                language="English",
                timezone="UTC",
                hmac_key="k2",
                created_at=_now(),
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


class TestDeployment:
    def test_defaults(self, db, user):
        dep = Deployment(
            user_id=user.id,
            bot_type="waha",
            voice="af_heart",
            goal="be helpful",
            language="English",
            created_at=_now(),
            updated_at=_now(),
        )
        db.add(dep)
        db.commit()
        db.refresh(dep)
        assert dep.status == "needs_connect"
        assert dep.desired_state == "stopped"
        assert dep.run_id is None
        assert dep.feature_flags == {}
        assert dep.settings == {}
        assert dep.template == "general"
        assert dep.tool_overrides == {}

    def test_unique_user_bot_type(self, db, user):
        db.add(
            Deployment(
                user_id=user.id,
                bot_type="waha",
                voice="af_heart",
                goal="g",
                language="English",
                created_at=_now(),
                updated_at=_now(),
            )
        )
        db.commit()
        db.add(
            Deployment(
                user_id=user.id,
                bot_type="waha",
                voice="af_heart",
                goal="g2",
                language="English",
                created_at=_now(),
                updated_at=_now(),
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()

    def test_json_column_roundtrip(self, db, user):
        dep = Deployment(
            user_id=user.id,
            bot_type="waha",
            voice="af_heart",
            goal="g",
            language="English",
            feature_flags={"image": True, "stt": False},
            settings={"whitelist": ["a", "b"], "participation": {"enabled": True}},
            created_at=_now(),
            updated_at=_now(),
        )
        db.add(dep)
        db.commit()
        dep_id = dep.id
        db.expire_all()

        reloaded = db.query(Deployment).filter(Deployment.id == dep_id).one()
        assert reloaded.feature_flags == {"image": True, "stt": False}
        assert reloaded.settings == {"whitelist": ["a", "b"], "participation": {"enabled": True}}


class TestConnection:
    def test_defaults(self, db, user):
        conn = Connection(
            user_id=user.id,
            service="whatsapp",
            created_at=_now(),
            updated_at=_now(),
        )
        db.add(conn)
        db.commit()
        db.refresh(conn)
        assert conn.status == "disconnected"
        assert conn.config == {}

    def test_unique_user_service(self, db, user):
        db.add(
            Connection(user_id=user.id, service="whatsapp", created_at=_now(), updated_at=_now())
        )
        db.commit()
        db.add(
            Connection(user_id=user.id, service="whatsapp", created_at=_now(), updated_at=_now())
        )
        with pytest.raises(IntegrityError):
            db.commit()


class TestLoginRequest:
    def test_defaults(self, db, user):
        req = LoginRequest(user_id=user.id, created_at=_now())
        db.add(req)
        db.commit()
        db.refresh(req)
        assert req.status == "pending"
        assert req.fulfilled_at is None
        assert req.token_id is None


class TestLoginToken:
    def test_defaults(self, db, user):
        token = LoginToken(
            token="abc123",
            user_id=user.id,
            created_at=_now(),
            expires_at=_now(),
        )
        db.add(token)
        db.commit()
        db.refresh(token)
        assert token.consumed_at is None

    def test_token_unique(self, db, user):
        db.add(LoginToken(token="dup", user_id=user.id, created_at=_now(), expires_at=_now()))
        db.commit()
        db.add(LoginToken(token="dup", user_id=user.id, created_at=_now(), expires_at=_now()))
        with pytest.raises(IntegrityError):
            db.commit()
