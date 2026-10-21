"""Tests for the time and weather tools."""

import httpx

from kai.agent.tools import time as time_tool
from kai.agent.tools.time import get_time_in_timezone, get_weather


class TestGetTime:
    def test_returns_utc_and_local_without_timezone(self):
        result = get_time_in_timezone()
        assert {"utc", "local"} <= set(result)
        assert result["utc"].endswith("UTC")

    def test_includes_requested_timezone(self):
        result = get_time_in_timezone("Europe/Berlin")
        assert "Europe/Berlin" in result
        assert "error" not in result

    def test_unknown_timezone_is_reported(self):
        result = get_time_in_timezone("Not/AZone")
        assert result["error"] == "Unknown timezone: Not/AZone"


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _FakeClient:
    def __init__(self, *, payload: object = None, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self._error is not None:
            raise self._error
        return _FakeResponse(self._payload)


class TestGetWeather:
    def _patch_client(self, monkeypatch, client) -> None:
        monkeypatch.setattr(time_tool.httpx, "Client", lambda **kwargs: client)

    def test_formats_current_conditions(self, monkeypatch):
        payload = {
            "current_condition": [
                {
                    "weatherDesc": [{"value": "Sunny"}],
                    "temp_C": "21",
                    "FeelsLikeC": "20",
                    "humidity": "40",
                    "windspeedKmph": "9",
                }
            ],
            "nearest_area": [{"areaName": [{"value": "Berlin"}]}],
        }
        client = _FakeClient(payload=payload)
        self._patch_client(monkeypatch, client)

        result = get_weather("Berlin")

        assert result is not None
        assert result.startswith("Berlin: Sunny, 21°C")
        assert len(client.calls) == 1

    def test_missing_area_falls_back_to_requested_location(self, monkeypatch):
        payload = {
            "current_condition": [
                {
                    "weatherDesc": [{"value": "Rain"}],
                    "temp_C": "10",
                    "FeelsLikeC": "9",
                    "humidity": "80",
                    "windspeedKmph": "4",
                }
            ],
        }
        self._patch_client(monkeypatch, _FakeClient(payload=payload))

        result = get_weather("Hamburg")
        assert result is not None
        assert result.startswith("Hamburg: Rain")

    def test_http_failure_returns_none(self, monkeypatch):
        self._patch_client(monkeypatch, _FakeClient(error=httpx.HTTPError("down")))

        assert get_weather("Berlin") is None

    def test_malformed_payload_returns_none(self, monkeypatch):
        self._patch_client(monkeypatch, _FakeClient(payload={}))

        assert get_weather("Berlin") is None
