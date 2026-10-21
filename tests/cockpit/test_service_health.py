"""Tests for the cockpit service-health probes."""

from types import SimpleNamespace
from typing import cast

import httpx
import pytest

from kai.cockpit import service_health


def _as_client(fake: "_FakeClient") -> httpx.AsyncClient:
    """Present the duck-typed fake as an AsyncClient for the type checker."""
    return cast(httpx.AsyncClient, fake)


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeClient:
    def __init__(self, *, response: _FakeResponse | None = None, error: Exception | None = None):
        self.response = response or _FakeResponse(200)
        self.error = error
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, headers=None):
        self.calls.append({"url": url, "headers": headers})
        if self.error is not None:
            raise self.error
        return self.response


class TestProbe:
    @pytest.mark.asyncio
    async def test_200_is_healthy(self):
        ok, detail = await service_health.probe(_as_client(_FakeClient()), "http://x")
        assert (ok, detail) == (True, "responding")

    @pytest.mark.asyncio
    async def test_non_200_reports_status(self):
        ok, detail = await service_health.probe(
            _as_client(_FakeClient(response=_FakeResponse(503))), "http://x"
        )
        assert (ok, detail) == (False, "HTTP 503")

    @pytest.mark.asyncio
    async def test_connect_error_is_not_reachable(self):
        client = _FakeClient(error=httpx.ConnectError("no route"))
        ok, detail = await service_health.probe(_as_client(client), "http://x")
        assert (ok, detail) == (False, "not reachable")

    @pytest.mark.asyncio
    async def test_timeout_reports_exception_type(self):
        client = _FakeClient(error=httpx.ReadTimeout("slow"))
        ok, detail = await service_health.probe(_as_client(client), "http://x")
        assert (ok, detail) == (False, "ReadTimeout")


class TestIndividualChecks:
    @pytest.mark.asyncio
    async def test_llm_without_key_is_skipped(self, monkeypatch):
        monkeypatch.setattr(
            service_health,
            "get_settings",
            lambda: SimpleNamespace(llm_api_key="", llm_api_base="http://llm/v1"),
        )
        check = await service_health.check_llm(_as_client(_FakeClient()))
        assert check.ok is False
        assert check.detail == "no API key configured"

    @pytest.mark.asyncio
    async def test_llm_probes_models_endpoint(self, monkeypatch):
        monkeypatch.setattr(
            service_health,
            "get_settings",
            lambda: SimpleNamespace(llm_api_key="sk-test", llm_api_base="http://llm/v1/"),
        )
        client = _FakeClient()
        check = await service_health.check_llm(_as_client(client))
        assert check == service_health.HealthCheck("LLM API", True, "responding")
        assert client.calls[0]["url"] == "http://llm/v1/models"
        assert client.calls[0]["headers"] == {"Authorization": "Bearer sk-test"}

    @pytest.mark.asyncio
    async def test_morphik_sends_bearer_only_with_token(self):
        client = _FakeClient()
        with_token = await service_health.check_morphik(
            _as_client(client), "http://morphik/", "tok"
        )
        assert with_token.label == "RAG Server"
        assert client.calls[0]["headers"] == {"Authorization": "Bearer tok"}
        assert client.calls[0]["url"] == "http://morphik/health"

        without_token = await service_health.check_morphik(_as_client(client), "http://morphik", "")
        assert without_token.ok is True
        assert client.calls[1]["headers"] is None

    @pytest.mark.asyncio
    async def test_crawler_probes_health_endpoint(self):
        client = _FakeClient()
        check = await service_health.check_crawl4ai(_as_client(client), "http://crawl/", "tok")
        assert check.label == "Crawler"
        assert client.calls[0]["url"] == "http://crawl/health"
        assert client.calls[0]["headers"] == {"Authorization": "Bearer tok"}


class TestCrawlerHealth:
    @pytest.mark.asyncio
    async def test_config_error_returns_none(self, monkeypatch):
        def broken():
            raise RuntimeError("not configured")

        monkeypatch.setattr(service_health, "get_brain_settings", broken)
        assert await service_health.check_crawler_health() is None

    @pytest.mark.asyncio
    async def test_missing_crawler_url_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            service_health,
            "get_brain_settings",
            lambda: SimpleNamespace(crawler_url="", crawl4ai_token=""),
        )
        assert await service_health.check_crawler_health() is None

    @pytest.mark.asyncio
    async def test_configured_crawler_is_probed(self, monkeypatch):
        monkeypatch.setattr(
            service_health,
            "get_brain_settings",
            lambda: SimpleNamespace(crawler_url="http://crawl", crawl4ai_token="tok"),
        )
        monkeypatch.setattr(service_health.httpx, "AsyncClient", lambda **kwargs: _FakeClient())
        check = await service_health.check_crawler_health()
        assert check is not None
        assert check.ok is True


class TestAggregation:
    @pytest.mark.asyncio
    async def test_gather_keeps_checks_and_converts_exceptions(self):
        async def successful():
            return service_health.HealthCheck("ok", True, "fine")

        async def broken():
            raise RuntimeError("boom")

        checks = await service_health.gather_probes([successful(), broken()])
        assert checks[0].label == "ok"
        assert checks[1].label == "Probe"
        assert checks[1].detail == "RuntimeError"

    @pytest.mark.asyncio
    async def test_service_health_reports_config_error(self, monkeypatch):
        monkeypatch.setattr(service_health.httpx, "AsyncClient", lambda **kwargs: _FakeClient())
        monkeypatch.setattr(
            service_health,
            "get_settings",
            lambda: SimpleNamespace(llm_api_key="sk", llm_api_base="http://llm/v1"),
        )

        def broken():
            raise RuntimeError("bad brain config")

        monkeypatch.setattr(service_health, "get_brain_settings", broken)
        checks = await service_health.check_service_health()
        labels = {check.label for check in checks}
        assert "LLM API" in labels
        assert "Brain" in labels

    @pytest.mark.asyncio
    async def test_service_health_probes_enabled_brain_services(self, monkeypatch):
        monkeypatch.setattr(service_health.httpx, "AsyncClient", lambda **kwargs: _FakeClient())
        monkeypatch.setattr(
            service_health,
            "get_settings",
            lambda: SimpleNamespace(llm_api_key="sk", llm_api_base="http://llm/v1"),
        )
        monkeypatch.setattr(
            service_health,
            "get_brain_settings",
            lambda: SimpleNamespace(
                base_url="http://morphik",
                morphik_token="m",
                crawler_url="http://crawl",
                crawl4ai_token="c",
            ),
        )

        checks = await service_health.check_service_health()

        assert {check.label for check in checks} == {"LLM API", "RAG Server", "Crawler"}
