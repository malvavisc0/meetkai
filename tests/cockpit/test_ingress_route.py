"""Tests for the reordered ingress route (02) and the resend WebhookType (03).

02 covers: body-size cap (413), missing-connection-row 404, per-operator
secret verification, nonce dedup (202) via the route-owned helpers, and
nonce-not-recorded-on-502 (so provider retries re-forward).

03 covers: ``_verify_resend`` (svix scheme: good/bad signature, stale
timestamp, missing headers, base64 secret), ``_parse_resend`` (text part,
HTML fallback, attachments), the ``NormalizedMessage`` contract, and
``is_nonce_seen``/``record_nonce`` helpers.
"""

import base64
import hashlib
import hmac
import time

import pytest

from kai.cockpit.bots import BOT_TYPES, BotType
from kai.cockpit.deployments import DeploymentsService
from kai.cockpit.models import Connection, Deployment, User
from kai.cockpit.naming import kai_slug_for
from kai.cockpit.secrets import encrypt_config
from kai.cockpit.webhooks import (
    WEBHOOK_TYPES,
    NormalizedMessage,
    _clear_seen_nonces,
    _parse_resend,
    _verify_resend,
    is_nonce_seen,
    record_nonce,
)

_KEY = "a" * 64


@pytest.fixture
def alice(db):
    u = User(
        email="alice@test.com",
        language="English",
        timezone="UTC",
        hmac_key="alice-hmac-key",
        created_at="now",
        is_disabled=False,
        kai_slug=kai_slug_for("alice@test.com"),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _resend_conn(user_id: int, secret: str) -> Connection:
    cfg = encrypt_config("resend", {"signing_secret": secret})
    return Connection(
        user_id=user_id,
        service="resend",
        status="connected",
        config=cfg,
        created_at="now",
        updated_at="now",
    )


@pytest.fixture(autouse=True)
def _clean_nonces():
    _clear_seen_nonces()
    yield
    _clear_seen_nonces()


@pytest.fixture(autouse=True)
def _encryption_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAI_CREDENTIAL_ENCRYPTION_KEY", _KEY)
    monkeypatch.setenv("KAI_CREDENTIAL_KEY_VERSION", "v1")
    from kai.cockpit import secrets as secrets_mod

    secrets_mod._clear_key_cache()
    yield
    secrets_mod._clear_key_cache()


def _svix_sign(secret: str, svix_id: str, ts: int, body: bytes) -> str:
    """Produce a valid svix-signature header value for a (secret, body)."""
    key = base64.b64decode(secret)
    signed = f"{svix_id}.{ts}.".encode() + body
    mac = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return f"v1,{mac}"


# ---------------------------------------------------------------------------
# 03: _verify_resend
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Minimal stand-in for Starlette Request — only headers are read by
    _verify_resend (the body is passed separately as the 2nd positional arg)."""

    def __init__(self, headers: dict[str, str]):
        self._headers = headers

    @property
    def headers(self):
        return self._headers


class TestVerifyResend:
    def test_resend_registered(self):
        assert "resend" in WEBHOOK_TYPES
        assert WEBHOOK_TYPES["resend"].nonce_header == "svix-id"

    def test_valid_signature_accepted(self):
        secret = base64.b64encode(b"raw-key-bytes").decode()
        body = b'{"from":"a@b.com","text":"hi"}'
        ts = int(time.time())
        sig = _svix_sign(secret, "msg-1", ts, body)
        req = _FakeRequest(
            {"svix-id": "msg-1", "svix-timestamp": str(ts), "svix-signature": sig},
        )
        assert _verify_resend(req, body, secret) is True

    def test_bad_signature_rejected(self):
        secret = base64.b64encode(b"raw-key-bytes").decode()
        body = b'{"from":"a@b.com"}'
        ts = int(time.time())
        req = _FakeRequest(
            {"svix-id": "msg-1", "svix-timestamp": str(ts), "svix-signature": "v1,deadbeef=="},
        )
        assert _verify_resend(req, body, secret) is False

    def test_stale_timestamp_rejected(self):
        secret = base64.b64encode(b"raw-key-bytes").decode()
        body = b'{"from":"a@b.com"}'
        ts = int(time.time()) - 600  # outside ±5 min window
        sig = _svix_sign(secret, "msg-1", ts, body)
        req = _FakeRequest(
            {"svix-id": "msg-1", "svix-timestamp": str(ts), "svix-signature": sig},
        )
        assert _verify_resend(req, body, secret) is False

    def test_missing_headers_rejected(self):
        req = _FakeRequest({})
        assert _verify_resend(req, b"{}", "secret") is False

    def test_non_integer_timestamp_rejected(self):
        req = _FakeRequest(
            {"svix-id": "x", "svix-timestamp": "not-a-number", "svix-signature": "v1,x"},
        )
        assert _verify_resend(req, b"{}", base64.b64encode(b"k").decode()) is False

    def test_invalid_base64_secret_rejected(self):
        ts = int(time.time())
        req = _FakeRequest(
            {"svix-id": "x", "svix-timestamp": str(ts), "svix-signature": "v1,x"},
        )
        # secret that isn't valid base64 → b64decode fails → False
        assert _verify_resend(req, b"{}", "not!!!base64!!!") is False

    def test_verify_records_no_nonce(self):
        # verify_signature must NOT record the nonce — the route owns dedup.
        secret = base64.b64encode(b"raw-key-bytes").decode()
        body = b'{"from":"a@b.com"}'
        ts = int(time.time())
        sig = _svix_sign(secret, "msg-nonce", ts, body)
        req = _FakeRequest(
            {"svix-id": "msg-nonce", "svix-timestamp": str(ts), "svix-signature": sig},
        )
        assert _verify_resend(req, body, secret) is True
        # the svix-id is NOT in the nonce set after verify alone
        assert is_nonce_seen("msg-nonce") is False

    def test_multiple_signature_candidates(self):
        # svix-signature is a space-separated list; one valid candidate passes.
        secret = base64.b64encode(b"raw-key-bytes").decode()
        body = b'{"from":"a@b.com"}'
        ts = int(time.time())
        good = _svix_sign(secret, "msg-1", ts, body)
        sig = f"v1,deadbeef== {good}"
        req = _FakeRequest(
            {"svix-id": "msg-1", "svix-timestamp": str(ts), "svix-signature": sig},
        )
        assert _verify_resend(req, body, secret) is True


# ---------------------------------------------------------------------------
# 03: _parse_resend
# ---------------------------------------------------------------------------


class TestParseResend:
    """``_parse_resend`` maps Resend's real webhook shape.

    The webhook body is ``{"type": ..., "created_at": ..., "data": {...}}``
    — envelope metadata only. Body text/HTML and attachment download URLs
    require follow-up calls to the Received Emails / Attachments REST APIs
    (monkeypatched here via ``kai.cockpit.webhooks._fetch_resend_email`` /
    ``_fetch_resend_attachments``), authenticated with the connection's
    ``api_key``.
    """

    def _payload(self, **data_overrides):
        data = {
            "email_id": "email-123",
            "created_at": "2026-01-01T00:00:00Z",
            "from": "a@b.com",
            "to": ["support@meetk.ai"],
            "bcc": [],
            "cc": [],
            "received_for": ["support@meetk.ai"],
            "message_id": "msg-42",
            "subject": "Question",
            "attachments": [],
        }
        data.update(data_overrides)
        return {"type": "email.received", "created_at": "2026-01-01T00:00:00Z", "data": data}

    def test_text_part_preferred(self, monkeypatch):
        import kai.cockpit.webhooks as webhooks_mod

        monkeypatch.setattr(
            webhooks_mod,
            "_fetch_resend_email",
            lambda email_id, api_key: {"text": "hello", "html": "<p>x</p>"},
        )
        msg = _parse_resend(self._payload(), {"api_key": "re_test"})
        assert msg.source == "a@b.com"
        assert msg.text == "hello"
        assert msg.event == "email.inbound"

    def test_html_fallback_stripped(self, monkeypatch):
        import kai.cockpit.webhooks as webhooks_mod

        monkeypatch.setattr(
            webhooks_mod,
            "_fetch_resend_email",
            lambda email_id, api_key: {"html": "<p>Hello <b>world</b></p>"},
        )
        msg = _parse_resend(self._payload(), {"api_key": "re_test"})
        assert "Hello" in msg.text
        assert "<p>" not in msg.text

    def test_empty_body_when_no_text_or_html(self, monkeypatch):
        import kai.cockpit.webhooks as webhooks_mod

        monkeypatch.setattr(webhooks_mod, "_fetch_resend_email", lambda email_id, api_key: {})
        msg = _parse_resend(self._payload(), {"api_key": "re_test"})
        assert msg.text == ""

    def test_metadata_fields(self, monkeypatch):
        import kai.cockpit.webhooks as webhooks_mod

        monkeypatch.setattr(
            webhooks_mod, "_fetch_resend_email", lambda email_id, api_key: {"text": "hi"}
        )
        msg = _parse_resend(self._payload(), {"api_key": "re_test"})
        assert msg.metadata["message_id"] == "msg-42"
        assert msg.metadata["subject"] == "Question"
        assert msg.metadata["to"] == ["support@meetk.ai"]

    def test_attachments_extracted_as_urls_only(self, monkeypatch):
        import kai.cockpit.webhooks as webhooks_mod

        monkeypatch.setattr(
            webhooks_mod, "_fetch_resend_email", lambda email_id, api_key: {"text": "see attached"}
        )
        monkeypatch.setattr(
            webhooks_mod,
            "_fetch_resend_attachments",
            lambda email_id, api_key: [
                {
                    "download_url": "https://resend.example/att1",
                    "content_type": "image/png",
                    "filename": "shot.png",
                },
            ],
        )
        msg = _parse_resend(
            self._payload(attachments=[{"id": "att-1", "filename": "shot.png"}]),
            {"api_key": "re_test"},
        )
        atts = msg.metadata["attachments"]
        assert len(atts) == 1
        assert atts[0]["url"] == "https://resend.example/att1"
        assert atts[0]["content_type"] == "image/png"
        assert atts[0]["filename"] == "shot.png"

    def test_no_attachments_skips_attachments_api_call(self, monkeypatch):
        import kai.cockpit.webhooks as webhooks_mod

        monkeypatch.setattr(
            webhooks_mod, "_fetch_resend_email", lambda email_id, api_key: {"text": "hi"}
        )

        def _boom(email_id, api_key):
            raise AssertionError("attachments API should not be called")

        monkeypatch.setattr(webhooks_mod, "_fetch_resend_attachments", _boom)
        msg = _parse_resend(self._payload(attachments=[]), {"api_key": "re_test"})
        assert msg.metadata["attachments"] == []

    def test_non_inbound_event_skips_api_calls(self, monkeypatch):
        import kai.cockpit.webhooks as webhooks_mod

        def _boom(*args, **kwargs):
            raise AssertionError("no API call should be made for a non-inbound event")

        monkeypatch.setattr(webhooks_mod, "_fetch_resend_email", _boom)
        monkeypatch.setattr(webhooks_mod, "_fetch_resend_attachments", _boom)
        payload = {"type": "email.sent", "created_at": "x", "data": {"from": "a@b.com"}}
        msg = _parse_resend(payload, {"api_key": "re_test"})
        assert msg.event == "email.sent"
        assert msg.text == ""

    def test_upstream_api_error_is_raised(self, monkeypatch):
        import kai.cockpit.webhooks as webhooks_mod

        def _fail(email_id, api_key):
            raise webhooks_mod.WebhookUpstreamError("boom")

        monkeypatch.setattr(webhooks_mod, "_fetch_resend_email", _fail)
        with pytest.raises(webhooks_mod.WebhookUpstreamError):
            _parse_resend(self._payload(), {"api_key": "re_test"})

    def test_normalized_message_contract_fields(self):
        msg = NormalizedMessage(source="s", text="t", metadata={"k": 1}, event="e")
        assert set(msg.model_dump()) == {"source", "text", "metadata", "event"}


# ---------------------------------------------------------------------------
# 02: is_nonce_seen / record_nonce helpers
# ---------------------------------------------------------------------------


class TestNonceHelpers:
    def test_unseen_nonce_returns_false(self):
        assert is_nonce_seen("never-seen") is False

    def test_record_then_seen(self):
        # record with real time so is_nonce_seen's real-time prune keeps it
        record_nonce("abc")
        assert is_nonce_seen("abc") is True

    def test_record_prunes_stale(self):
        # an old recorded nonce past the freshness window is pruned on the
        # next is_nonce_seen call (which uses real time.time())
        record_nonce("stale", now=0.0)
        assert is_nonce_seen("stale") is False

    def test_record_bounds_to_max(self, monkeypatch):
        monkeypatch.setattr("kai.cockpit.webhooks._SEEN_NONCES_MAX", 3)
        record_nonce("a")
        record_nonce("b")
        record_nonce("c")
        record_nonce("d")
        assert is_nonce_seen("a") is False  # oldest dropped
        assert is_nonce_seen("d") is True


# ---------------------------------------------------------------------------
# 02: route-level behaviors (via the FastAPI client)
# ---------------------------------------------------------------------------


def _email_bot(monkeypatch) -> BotType:
    fake_bt = BotType(
        name="email",
        feature_flags=[],
        required_connections=["resend"],
        supported_connections=[],
    )
    monkeypatch.setitem(BOT_TYPES, "email", fake_bt)
    return fake_bt


def _running_email_dep(db, alice) -> Deployment:
    dep = Deployment(
        user_id=alice.id,
        bot_type="email",
        run_id="fake-run",
        status="running",
        desired_state="running",
        voice="af_heart",
        goal="answer email",
        language="English",
        feature_flags={},
        settings={},
        created_at="now",
        updated_at="now",
    )
    db.add(dep)
    db.commit()
    return dep


class TestRouteBodyCap:
    def test_oversized_body_returns_413(self, client, alice, monkeypatch):
        _email_bot(monkeypatch)
        # body cap is step 1 (before type/user/connection lookup), so no
        # connection or deployment is needed — the oversized payload is
        # rejected before any DB work.
        big = b"x" * (1 * 1024 * 1024 + 1)
        r = client.post(
            f"/webhook/{alice.kai_slug}/resend",
            content=big,
        )
        assert r.status_code == 413


class TestRouteMissingConnection:
    def test_no_connection_row_returns_404(self, client, alice, monkeypatch):
        _email_bot(monkeypatch)
        # resend type exists, user exists, but no Connection row → 404
        r = client.post(
            f"/webhook/{alice.kai_slug}/resend",
            json={"text": "hi"},
        )
        assert r.status_code == 404


class TestRoutePerOpSecretAndNonce:
    def test_bad_signature_returns_401(self, client, db, alice, monkeypatch):
        _email_bot(monkeypatch)
        db.add(_resend_conn(alice.id, base64.b64encode(b"real-key").decode()))
        db.commit()
        ts = int(time.time())
        r = client.post(
            f"/webhook/{alice.kai_slug}/resend",
            content=b'{"from":"a@b.com","text":"hi"}',
            headers={
                "svix-id": "msg-1",
                "svix-timestamp": str(ts),
                "svix-signature": "v1,wrong==",
            },
        )
        assert r.status_code == 401

    def test_valid_signature_no_deployment_returns_404(self, client, db, alice, monkeypatch):
        _email_bot(monkeypatch)
        secret = base64.b64encode(b"raw-key").decode()
        db.add(_resend_conn(alice.id, secret))
        db.commit()
        body = b'{"from":"a@b.com","text":"hi"}'
        ts = int(time.time())
        r = client.post(
            f"/webhook/{alice.kai_slug}/resend",
            content=body,
            headers={
                "svix-id": "msg-1",
                "svix-timestamp": str(ts),
                "svix-signature": _svix_sign(secret, "msg-1", ts, body),
            },
        )
        assert r.status_code == 404  # no running deployment

    def test_success_returns_202_and_records_nonce(
        self, client, db, alice, monkeypatch
    ):
        _email_bot(monkeypatch)
        secret = base64.b64encode(b"raw-key").decode()
        db.add(_resend_conn(alice.id, secret))
        _running_email_dep(db, alice)
        db.commit()

        monkeypatch.setattr(DeploymentsService, "forward_event", lambda self, d, p, b: True)

        body = b'{"from":"a@b.com","text":"hi"}'
        ts = int(time.time())
        r = client.post(
            f"/webhook/{alice.kai_slug}/resend",
            content=body,
            headers={
                "svix-id": "msg-unique",
                "svix-timestamp": str(ts),
                "svix-signature": _svix_sign(secret, "msg-unique", ts, body),
            },
        )
        assert r.status_code == 202
        assert r.json() == {"ok": True}
        # nonce recorded after successful forward
        assert is_nonce_seen("msg-unique") is True

    def test_duplicate_nonce_dedups_to_202(self, client, db, alice, monkeypatch):
        _email_bot(monkeypatch)
        secret = base64.b64encode(b"raw-key").decode()
        db.add(_resend_conn(alice.id, secret))
        _running_email_dep(db, alice)
        db.commit()

        forward_count = 0

        def _forward(self, d, p, b):
            nonlocal forward_count
            forward_count += 1
            return True

        monkeypatch.setattr(DeploymentsService, "forward_event", _forward)

        body = b'{"from":"a@b.com","text":"hi"}'
        ts = int(time.time())
        headers = {
            "svix-id": "msg-dup",
            "svix-timestamp": str(ts),
            "svix-signature": _svix_sign(secret, "msg-dup", ts, body),
        }
        r1 = client.post(
            f"/webhook/{alice.kai_slug}/resend", content=body, headers=headers
        )
        assert r1.status_code == 202
        assert forward_count == 1

        # second identical request — same svix-id, fresh timestamp+sig
        ts2 = int(time.time())
        r2 = client.post(
            f"/webhook/{alice.kai_slug}/resend",
            content=body,
            headers={
                "svix-id": "msg-dup",
                "svix-timestamp": str(ts2),
                "svix-signature": _svix_sign(secret, "msg-dup", ts2, body),
            },
        )
        assert r2.status_code == 202
        assert r2.json() == {"deduped": True}
        # not re-forwarded
        assert forward_count == 1

    def test_nonce_not_recorded_on_502(self, client, db, alice, monkeypatch):
        _email_bot(monkeypatch)
        secret = base64.b64encode(b"raw-key").decode()
        db.add(_resend_conn(alice.id, secret))
        _running_email_dep(db, alice)
        db.commit()

        monkeypatch.setattr(DeploymentsService, "forward_event", lambda self, d, p, b: False)

        body = b'{"from":"a@b.com","text":"hi"}'
        ts = int(time.time())
        r = client.post(
            f"/webhook/{alice.kai_slug}/resend",
            content=body,
            headers={
                "svix-id": "msg-fail",
                "svix-timestamp": str(ts),
                "svix-signature": _svix_sign(secret, "msg-fail", ts, body),
            },
        )
        assert r.status_code == 502
        # nonce NOT recorded → a retry of the same svix-id re-forwards
        assert is_nonce_seen("msg-fail") is False

    def test_retry_after_502_re_forwards(self, client, db, alice, monkeypatch):
        _email_bot(monkeypatch)
        secret = base64.b64encode(b"raw-key").decode()
        db.add(_resend_conn(alice.id, secret))
        _running_email_dep(db, alice)
        db.commit()

        results = [False, True]
        calls = 0

        def _forward(self, d, p, b):
            nonlocal calls
            ok = results[min(calls, len(results) - 1)]
            calls += 1
            return ok

        monkeypatch.setattr(DeploymentsService, "forward_event", _forward)

        body = b'{"from":"a@b.com","text":"hi"}'
        for i in range(2):
            ts = int(time.time())
            client.post(
                f"/webhook/{alice.kai_slug}/resend",
                content=body,
                headers={
                    "svix-id": "msg-retry",
                    "svix-timestamp": str(ts),
                    "svix-signature": _svix_sign(secret, "msg-retry", ts, body),
                },
            )
        # first failed (502), second succeeded (202) and recorded the nonce
        assert calls == 2
        assert is_nonce_seen("msg-retry") is True
