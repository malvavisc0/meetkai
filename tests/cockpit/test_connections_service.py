"""Tests for kai.cockpit.connections.service.ConnectionsService.

WAHA REST calls are mocked at the ``WahaClient`` method level
rather than via respx/httpx, since the service only
ever calls through the client's public methods.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kai.cockpit.connections.service import ConnectionsService
from kai.cockpit.models import Connection


@pytest.fixture
def fake_waha_client(monkeypatch):
    """Patch ``WahaClient`` used by connections.py with an AsyncMock."""
    client = AsyncMock()
    client.close = AsyncMock()
    monkeypatch.setattr("kai.cockpit.connections.service.WahaClient", lambda settings: client)
    monkeypatch.setattr(
        "kai.cockpit.connections.service.get_waha_settings",
        lambda: SimpleNamespace(webhook_public_host="test-host"),
    )
    return client


class TestLazyConnectionCreation:
    def test_get_or_create_creates_row(self, db, user):
        svc = ConnectionsService(db)
        assert svc.get_whatsapp(user) is None
        conn = svc.get_or_create_whatsapp(user)
        assert conn.service == "whatsapp"
        assert conn.status == "disconnected"
        # WAHA only allows [a-zA-Z0-9_-]; @ -> _at_, . -> _, version-pinned.
        # bob@test.com -> kai-v001-bob_at_test_com
        assert conn.config["waha_session"] == "kai-v001-bob_at_test_com"

    def test_get_or_create_idempotent(self, db, user):
        svc = ConnectionsService(db)
        first = svc.get_or_create_whatsapp(user)
        second = svc.get_or_create_whatsapp(user)
        assert first.id == second.id


class TestPortAllocation:
    def test_allocates_from_range(self, db, user, monkeypatch):
        monkeypatch.setenv("KAI_WAHA_WEBHOOK_PORT_RANGE", "9000-9002")
        svc = ConnectionsService(db)
        conn = svc.get_or_create_whatsapp(user)
        assert 9000 <= conn.config["waha_webhook_port"] <= 9002

    def test_skips_used_ports(self, db, user, monkeypatch):
        monkeypatch.setenv("KAI_WAHA_WEBHOOK_PORT_RANGE", "9000-9002")
        db.add(
            Connection(
                user_id=999,
                service="whatsapp",
                config={"waha_webhook_port": 9000},
                created_at="now",
                updated_at="now",
            )
        )
        db.commit()
        svc = ConnectionsService(db)
        conn = svc.get_or_create_whatsapp(user)
        assert conn.config["waha_webhook_port"] == 9001

    def test_raises_when_range_exhausted(self, db, user, monkeypatch):
        monkeypatch.setenv("KAI_WAHA_WEBHOOK_PORT_RANGE", "9000-9000")
        db.add(
            Connection(
                user_id=999,
                service="whatsapp",
                config={"waha_webhook_port": 9000},
                created_at="now",
                updated_at="now",
            )
        )
        db.commit()
        svc = ConnectionsService(db)
        with pytest.raises(RuntimeError):
            svc.get_or_create_whatsapp(user)


class TestConnectWhatsapp:
    @pytest.mark.asyncio
    async def test_connect_working_returns_connected(self, db, user, fake_waha_client):
        fake_waha_client.create_session.return_value = {}
        fake_waha_client.get_session.return_value = {"status": "WORKING"}

        svc = ConnectionsService(db)
        result = await svc.connect_whatsapp(user)

        assert result == {"status": "connected"}
        conn = svc.get_whatsapp(user)
        assert conn is not None
        assert conn.status == "connected"
        fake_waha_client.close.assert_awaited()

    @pytest.mark.asyncio
    async def test_connect_scan_qr_returns_qr_bytes(self, db, user, fake_waha_client):
        fake_waha_client.create_session.return_value = {}
        fake_waha_client.get_session.return_value = {"status": "SCAN_QR_CODE"}
        fake_waha_client.get_qr.return_value = b"png-bytes"

        svc = ConnectionsService(db)
        result = await svc.connect_whatsapp(user)

        assert result == {"status": "scan_qr", "qr_bytes": b"png-bytes"}

    @pytest.mark.asyncio
    async def test_connect_failed_raises(self, db, user, fake_waha_client):
        fake_waha_client.create_session.return_value = {}
        fake_waha_client.get_session.return_value = {"status": "FAILED"}

        svc = ConnectionsService(db)
        with pytest.raises(ConnectionError):
            await svc.connect_whatsapp(user)

        conn = svc.get_whatsapp(user)
        assert conn is not None
        assert conn.status == "disconnected"


class TestRefreshStatus:
    @pytest.mark.asyncio
    async def test_refresh_working_sets_connected(self, db, user, fake_waha_client):
        svc = ConnectionsService(db)
        svc.get_or_create_whatsapp(user)
        fake_waha_client.get_session.return_value = {"status": "WORKING"}

        conn = await svc.refresh_status(user)
        assert conn.status == "connected"

    @pytest.mark.asyncio
    async def test_refresh_missing_session_sets_disconnected(self, db, user, fake_waha_client):
        svc = ConnectionsService(db)
        svc.get_or_create_whatsapp(user)
        fake_waha_client.get_session.return_value = None

        conn = await svc.refresh_status(user)
        assert conn.status == "disconnected"

    @pytest.mark.asyncio
    async def test_refresh_creates_connection_if_missing(self, db, user, fake_waha_client):
        svc = ConnectionsService(db)
        assert svc.get_whatsapp(user) is None
        conn = await svc.refresh_status(user)
        assert conn is not None


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_deletes_session_and_marks_disconnected(
        self, db, user, fake_waha_client
    ):
        svc = ConnectionsService(db)
        conn = svc.get_or_create_whatsapp(user)
        conn.status = "connected"
        db.commit()

        await svc.disconnect_whatsapp(user)

        fake_waha_client.delete_session.assert_awaited_once_with(conn.config["waha_session"])
        assert conn.status == "disconnected"

    @pytest.mark.asyncio
    async def test_disconnect_noop_when_no_connection(self, db, user, fake_waha_client):
        svc = ConnectionsService(db)
        await svc.disconnect_whatsapp(user)
        fake_waha_client.delete_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disconnect_preserves_connection_row(self, db, user, fake_waha_client):
        svc = ConnectionsService(db)
        conn = svc.get_or_create_whatsapp(user)
        conn.status = "connected"
        db.commit()
        conn_id = conn.id

        await svc.disconnect_whatsapp(user)

        disconnected = svc.get_whatsapp(user)
        assert disconnected is not None
        assert disconnected.id == conn_id
