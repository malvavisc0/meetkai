import json

import pytest
import respx
from httpx import Response

from kai.brain.crawler import Crawl4aiClient, CrawlLinks, CrawlPage, MarkdownResult


@pytest.fixture
def client(settings):
    return Crawl4aiClient(settings)


class TestExtractMarkdown:
    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_markdown_string(self, client):
        respx.post("/md").mock(
            return_value=Response(
                200,
                json={
                    "url": "https://transmissionbt.com/",
                    "filter": "fit",
                    "markdown": "# Transmission\nA Fast, Easy and Free Bittorrent Client",
                    "success": True,
                },
            )
        )
        md = await client.extract_markdown(url="https://transmissionbt.com/")
        assert isinstance(md, str)
        assert "Transmission" in md

        body = json.loads(respx.calls[0].request.content)
        assert body["f"] == "fit"
        assert body["url"] == "https://transmissionbt.com/"

    @respx.mock
    @pytest.mark.asyncio
    async def test_strategy_override(self, client):
        respx.post("/md").mock(
            return_value=Response(200, json={"markdown": "raw content", "success": True})
        )
        await client.extract_markdown(url="https://x.com/", strategy="raw")
        body = json.loads(respx.calls[0].request.content)
        assert body["f"] == "raw"

    @respx.mock
    @pytest.mark.asyncio
    async def test_raises_on_http_error(self, client):
        respx.post("/md").mock(return_value=Response(401))
        with pytest.raises(Exception):
            await client.extract_markdown(url="https://x.com/")


class TestCrawl:
    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_markdown_dict_and_links(self, client):
        respx.post("/crawl").mock(
            return_value=Response(
                200,
                json={
                    "success": True,
                    "results": [
                        {
                            "url": "https://transmissionbt.com/",
                            "success": True,
                            "markdown": {
                                "raw_markdown": "# Transmission",
                                "markdown_with_citations": "...",
                                "references_markdown": "...",
                                "fit_markdown": "",
                            },
                            "links": {
                                "internal": [
                                    {"href": "https://transmissionbt.com/download"},
                                    {"href": "https://transmissionbt.com/addons"},
                                ],
                                "external": [],
                            },
                        }
                    ],
                    "server_processing_time_s": 2.3,
                },
            )
        )
        page = await client.crawl(url="https://transmissionbt.com/")
        assert isinstance(page, CrawlPage)
        assert page.success is True
        assert page.url == "https://transmissionbt.com/"
        assert isinstance(page.markdown, MarkdownResult)
        assert page.markdown.raw_markdown == "# Transmission"
        assert page.markdown.fit_markdown == ""  # empty via /crawl
        assert isinstance(page.links, CrawlLinks)
        assert page.links.internal == [
            "https://transmissionbt.com/download",
            "https://transmissionbt.com/addons",
        ]
        assert page.links.external == []

        body = json.loads(respx.calls[0].request.content)
        cfg = body["crawler_config"]
        assert cfg["cache_mode"] == "bypass"
        assert cfg["exclude_external_links"] is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_handles_failed_page(self, client):
        respx.post("/crawl").mock(
            return_value=Response(
                200,
                json={
                    "success": False,
                    "results": [
                        {
                            "url": "https://broken.example/",
                            "success": False,
                            "error_message": "Navigation timeout",
                            "markdown": {},
                            "links": {},
                        }
                    ],
                },
            )
        )
        page = await client.crawl(url="https://broken.example/")
        assert page.success is False
        assert page.error_message == "Navigation timeout"
        assert page.markdown.raw_markdown == ""
        assert page.links.internal == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_synthesizes_failure_when_no_results(self, client):
        respx.post("/crawl").mock(
            return_value=Response(200, json={"success": False, "results": []})
        )
        page = await client.crawl(url="https://weird.example/")
        assert page.success is False
        assert "no results" in (page.error_message or "")

    @respx.mock
    @pytest.mark.asyncio
    async def test_defensive_string_markdown(self, client):
        """If /crawl ever returns markdown as a string (not dict), handle it."""
        respx.post("/crawl").mock(
            return_value=Response(
                200,
                json={
                    "success": True,
                    "results": [
                        {
                            "url": "https://x.com/",
                            "success": True,
                            "markdown": "# string markdown (defensive)",
                            "links": {"internal": [], "external": []},
                        }
                    ],
                },
            )
        )
        page = await client.crawl(url="https://x.com/")
        assert page.markdown.raw_markdown == "# string markdown (defensive)"


class TestHeaders:
    def test_bearer_token_header_set(self, settings):
        client = Crawl4aiClient(settings)
        assert client._client.headers["Authorization"] == "Bearer crawl4ai-test-token"

    def test_base_url_set(self, settings):
        client = Crawl4aiClient(settings)
        assert str(client._client.base_url).rstrip("/") == "http://localhost:11235"
