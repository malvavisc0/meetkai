import pytest
import respx
from httpx import Response

from kai.bots.waha.client import WahaClient
from kai.bots.waha.config import WahaSettings


@pytest.fixture
def settings():
    return WahaSettings(
        _env_file=None,
        url="http://localhost:3000",
        api_key="test-key",
        session="default",
        hmac_key="secret",
    )


@pytest.fixture
def client(settings):
    return WahaClient(settings)


class TestGetSessions:
    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_sessions(self, client):
        respx.get("/api/sessions").mock(
            return_value=Response(200, json=[{"name": "default", "status": "WORKING"}])
        )
        result = await client.get_sessions()
        assert len(result) == 1
        assert result[0]["name"] == "default"

    @respx.mock
    @pytest.mark.asyncio
    async def test_raises_on_error(self, client):
        respx.get("/api/sessions").mock(return_value=Response(500))
        with pytest.raises(Exception):
            await client.get_sessions()


class TestGetSessionStatus:
    @respx.mock
    @pytest.mark.asyncio
    async def test_finds_session(self, client):
        respx.get("/api/sessions").mock(
            return_value=Response(
                200,
                json=[
                    {"name": "other", "status": "STOPPED"},
                    {"name": "default", "status": "WORKING"},
                ],
            )
        )
        result = await client.get_session_status()
        assert result is not None
        assert result["status"] == "WORKING"

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self, client):
        respx.get("/api/sessions").mock(
            return_value=Response(200, json=[{"name": "other", "status": "STOPPED"}])
        )
        result = await client.get_session_status()
        assert result is None


class TestSendMessage:
    @respx.mock
    @pytest.mark.asyncio
    async def test_sends_text(self, client):
        route = respx.post("/api/sendText").mock(return_value=Response(201, json={"id": "msg_123"}))
        result = await client.send_message("123@c.us", "hello")
        assert result["id"] == "msg_123"
        body = route.calls[0].request.content
        assert b"123@c.us" in body
        assert b"hello" in body

    @respx.mock
    @pytest.mark.asyncio
    async def test_sends_with_mentions(self, client):
        route = respx.post("/api/sendText").mock(return_value=Response(201, json={"id": "msg_123"}))
        result = await client.send_message("group@g.us", "hello @123", mentions=["123@lid"])
        assert result["id"] == "msg_123"
        body = route.calls[0].request.content
        assert b"mentions" in body
        assert b"123@lid" in body

    @respx.mock
    @pytest.mark.asyncio
    async def test_sends_without_mentions_key_when_none(self, client):
        route = respx.post("/api/sendText").mock(return_value=Response(201, json={"id": "msg_123"}))
        await client.send_message("123@c.us", "hello", mentions=None)
        body = route.calls[0].request.content
        assert b"mentions" not in body

    @respx.mock
    @pytest.mark.asyncio
    async def test_sends_without_mentions_key_when_empty(self, client):
        route = respx.post("/api/sendText").mock(return_value=Response(201, json={"id": "msg_123"}))
        await client.send_message("123@c.us", "hello", mentions=[])
        body = route.calls[0].request.content
        assert b"mentions" not in body


class TestGetChatParticipants:
    @respx.mock
    @pytest.mark.asyncio
    async def test_fetches_participants(self, client):
        respx.get("/api/default/groups/120363%40g.us/participants/v2").mock(
            return_value=Response(
                200,
                json=[
                    {"id": "12345@c.us", "name": "Alice"},
                    {"id": "67890@c.us", "name": "Bob"},
                ],
            )
        )
        result = await client.get_chat_participants("120363@g.us")
        assert len(result) == 2
        assert result[0]["id"] == "12345@c.us"

    @respx.mock
    @pytest.mark.asyncio
    async def test_url_encodes_chat_id(self, client):
        route = respx.get("/api/default/groups/1234567890-1234567890%40g.us/participants/v2").mock(
            return_value=Response(200, json=[])
        )
        await client.get_chat_participants("1234567890-1234567890@g.us")
        assert "%40g.us/participants/v2" in str(route.calls[0].request.url)
        assert "@g.us/participants/v2" not in str(route.calls[0].request.url).replace("%40", "")

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_empty_list_on_non_list_response(self, client):
        respx.get("/api/default/groups/g%40g.us/participants/v2").mock(
            return_value=Response(200, json={"unexpected": True})
        )
        result = await client.get_chat_participants("g@g.us")
        assert result == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_raises_on_error(self, client):
        respx.get("/api/default/groups/g%40g.us/participants/v2").mock(return_value=Response(500))
        with pytest.raises(Exception):
            await client.get_chat_participants("g@g.us")


class TestGetChatMessages:
    @respx.mock
    @pytest.mark.asyncio
    async def test_fetches_messages(self, client):
        respx.get("/api/default/chats/120363%40g.us/messages").mock(
            return_value=Response(
                200,
                json=[
                    {"id": "m2", "body": "second", "fromMe": False},
                    {"id": "m1", "body": "first", "fromMe": False},
                ],
            )
        )
        result = await client.get_chat_messages("120363@g.us", limit=2)
        assert len(result) == 2
        assert result[0]["id"] == "m2"

    @respx.mock
    @pytest.mark.asyncio
    async def test_url_encodes_chat_id(self, client):
        route = respx.get("/api/default/chats/1234567890-1234567890%40g.us/messages").mock(
            return_value=Response(200, json=[])
        )
        await client.get_chat_messages("1234567890-1234567890@g.us")
        url = str(route.calls[0].request.url)
        assert "%40g.us/messages" in url
        assert "@g.us/messages" not in url.replace("%40", "")

    @respx.mock
    @pytest.mark.asyncio
    async def test_sends_query_params(self, client):
        route = respx.get("/api/default/chats/g%40g.us/messages").mock(
            return_value=Response(200, json=[])
        )
        await client.get_chat_messages("g@g.us", limit=100, offset=50, download_media=True)
        params = route.calls[0].request.url.params
        assert params["limit"] == "100"
        assert params["offset"] == "50"
        assert params["sortOrder"] == "desc"
        assert params["downloadMedia"] == "true"
        assert params["merge"] == "true"

    @respx.mock
    @pytest.mark.asyncio
    async def test_defaults_download_media_false(self, client):
        route = respx.get("/api/default/chats/g%40g.us/messages").mock(
            return_value=Response(200, json=[])
        )
        await client.get_chat_messages("g@g.us")
        assert route.calls[0].request.url.params["downloadMedia"] == "false"

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_empty_list_on_non_list_response(self, client):
        respx.get("/api/default/chats/g%40g.us/messages").mock(
            return_value=Response(200, json={"unexpected": True})
        )
        result = await client.get_chat_messages("g@g.us")
        assert result == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_raises_on_error(self, client):
        respx.get("/api/default/chats/g%40g.us/messages").mock(return_value=Response(500))
        with pytest.raises(Exception):
            await client.get_chat_messages("g@g.us")


class TestGetProfile:
    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_profile(self, client):
        respx.get("/api/default/profile").mock(
            return_value=Response(200, json={"id": "123@c.us", "name": "Test"})
        )
        result = await client.get_profile()
        assert result["name"] == "Test"

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_none_on_error(self, client):
        respx.get("/api/default/profile").mock(return_value=Response(404))
        result = await client.get_profile()
        assert result is None


class TestApiKeyHeader:
    @respx.mock
    @pytest.mark.asyncio
    async def test_sends_api_key(self, settings):
        route = respx.get("/api/sessions").mock(return_value=Response(200, json=[]))
        client = WahaClient(settings)
        await client.get_sessions()
        assert route.calls[0].request.headers["X-Api-Key"] == "test-key"
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_api_key_when_empty(self):
        settings = WahaSettings(_env_file=None, url="http://localhost:3000", api_key="")
        route = respx.get("/api/sessions").mock(return_value=Response(200, json=[]))
        client = WahaClient(settings)
        await client.get_sessions()
        assert "X-Api-Key" not in route.calls[0].request.headers
        await client.close()


class TestDownloadMedia:
    @respx.mock
    @pytest.mark.asyncio
    async def test_downloads_with_auth(self, client):
        media_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        route = respx.get("/api/files/default/media.jpg").mock(
            return_value=Response(200, content=media_bytes)
        )
        result = await client.download_media("/api/files/default/media.jpg")
        assert result == media_bytes
        assert route.calls[0].request.headers["X-Api-Key"] == "test-key"

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_none_on_error(self, client):
        respx.get("/api/files/default/media.jpg").mock(return_value=Response(404))
        result = await client.download_media("/api/files/default/media.jpg")
        assert result is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_rejects_oversized_content_length(self, client):
        respx.get("/api/files/default/big.jpg").mock(
            return_value=Response(
                200, headers={"content-length": str(100 * 1024 * 1024)}, content=b""
            )
        )
        result = await client.download_media("/api/files/default/big.jpg", max_size_mb=10)
        assert result is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_aborts_download_exceeding_max_size(self, client):
        big_content = b"\x00" * (11 * 1024 * 1024)
        respx.get("/api/files/default/big.jpg").mock(
            return_value=Response(200, content=big_content)
        )
        result = await client.download_media("/api/files/default/big.jpg", max_size_mb=10)
        assert result is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_sends_auth_header(self, settings):
        route = respx.get("/api/files/default/media.jpg").mock(
            return_value=Response(200, content=b"data")
        )
        client = WahaClient(settings)
        await client.download_media("/api/files/default/media.jpg")
        assert route.calls[0].request.headers["X-Api-Key"] == "test-key"
        await client.close()
