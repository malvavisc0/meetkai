"""Aggregated service-health probes for the cockpit.

Returns a list of :class:`HealthCheck` for the external infrastructure a
deployment relies on (beyond the bot process itself, whose status is
already surfaced on the deployment page): the WAHA service, the LLM
provider, and the optional media + brain services.

Every check here probes the *service* itself (is it reachable at all),
never a specific operator's per-connection state. Per-user channel state
(e.g. "is this operator's WhatsApp session connected") is a Connections
concern, not an infrastructure-health concern, and is surfaced on the
Connections page instead — mixing the two here would make this module
depend on caller-supplied, per-user data instead of being self-contained
like every other check.

Probes are bounded by short timeouts and run concurrently, so a single
unreachable service never stalls the page.

Only services that are *expected* to run appear in the result — a service
disabled by config (e.g. ``kokoro_enabled=False``) is omitted rather than
flagged as down.
"""

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass

import httpx

from kai.bots.waha.config import get_waha_settings
from kai.brain.config import get_brain_settings
from kai.config.settings import get_settings

_TIMEOUT = 3.0


@dataclass
class HealthCheck:
    label: str
    ok: bool = False
    detail: str = ""


async def _probe(
    client: httpx.AsyncClient, url: str, *, headers: dict[str, str] | None = None
) -> tuple[bool, str]:
    """Single-shot GET; (ok, detail). 200 only."""
    try:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            return True, "responding"
        return False, f"HTTP {resp.status_code}"
    except httpx.ConnectError:
        return False, "not reachable"
    except (httpx.ReadTimeout, httpx.HTTPError) as exc:
        return False, type(exc).__name__


def _config_error(label: str, exc: Exception) -> HealthCheck:
    return HealthCheck(label=label, ok=False, detail=f"config error: {type(exc).__name__}")


async def _check_waha(client: httpx.AsyncClient, base_url: str, api_key: str) -> HealthCheck:
    headers = {"X-Api-Key": api_key} if api_key else None
    ok, detail = await _probe(client, f"{base_url.rstrip('/')}/health", headers=headers)
    return HealthCheck(label="WhatsApp service (WAHA)", ok=ok, detail=detail)


async def _check_llm(client: httpx.AsyncClient) -> HealthCheck:
    settings = get_settings()
    if not settings.llm_api_key:
        return HealthCheck(label="LLM API", detail="no API key configured")
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    ok, detail = await _probe(
        client, f"{settings.llm_api_base.rstrip('/')}/models", headers=headers
    )
    return HealthCheck(label="LLM API", ok=ok, detail=detail)


async def _check_whisper(client: httpx.AsyncClient, host: str, port: int) -> HealthCheck:
    ok, detail = await _probe(client, f"http://{host}:{port}/health")
    return HealthCheck(label="Speech to Text Service", ok=ok, detail=detail)


async def _check_kokoro(client: httpx.AsyncClient, host: str, port: int) -> HealthCheck:
    ok, detail = await _probe(client, f"http://{host}:{port}/health")
    return HealthCheck(label="Text to Speech Service", ok=ok, detail=detail)


async def _check_morphik(client: httpx.AsyncClient, base_url: str, token: str) -> HealthCheck:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    ok, detail = await _probe(client, f"{base_url.rstrip('/')}/health", headers=headers)
    return HealthCheck(label="RAG Server", ok=ok, detail=detail)


async def _check_crawl4ai(client: httpx.AsyncClient, crawler_url: str, token: str) -> HealthCheck:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    ok, detail = await _probe(client, f"{crawler_url.rstrip('/')}/health", headers=headers)
    return HealthCheck(label="Crawler", ok=ok, detail=detail)


async def check_crawler_health() -> HealthCheck | None:
    """Probe just the crawl4ai crawler, or None if Brain/crawler isn't configured.

    Used by the Brain page to gate the "Add a website" form — if the crawler
    container is down the operator can still upload/paste sources, but a
    website ingest would fail at crawl time, so the form is disabled with a
    message instead of letting the request fail after the fact.
    """
    try:
        bs = get_brain_settings()
    except Exception:  # noqa: BLE001 - brain not configured
        return None
    if not bs.crawler_url:
        return None
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        return await _check_crawl4ai(client, bs.crawler_url, bs.crawl4ai_token)


async def _gather_probes(probes: list[Awaitable[HealthCheck]]) -> list[HealthCheck]:
    results = await asyncio.gather(*probes, return_exceptions=True)
    checks: list[HealthCheck] = []
    for r in results:
        if isinstance(r, HealthCheck):
            checks.append(r)
        else:
            checks.append(HealthCheck(label="Probe", detail=f"{type(r).__name__}"))
    return checks


async def check_service_health() -> list[HealthCheck]:
    """Probe the external infrastructure the deployment relies on.

    The bot process itself is intentionally not probed here — its status
    is already shown on the deployment page. Per-user channel connection
    state (e.g. this operator's WhatsApp session) is likewise out of
    scope — see the Connections page for that.
    """
    checks: list[HealthCheck] = []

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        # --- Concurrent network probes ---
        probes: list[Awaitable[HealthCheck]] = [_check_llm(client)]

        try:
            waha = get_waha_settings()
            probes.append(_check_waha(client, waha.url, waha.api_key))
            if waha.whisper_server_mode:
                probes.append(
                    _check_whisper(client, waha.whisper_server_host, waha.whisper_server_port)
                )
            if waha.kokoro_enabled:
                probes.append(
                    _check_kokoro(client, waha.kokoro_server_host, waha.kokoro_server_port)
                )
        except Exception as exc:  # noqa: BLE001 - config error shouldn't kill the card
            checks.append(_config_error("WAHA / media services", exc))

        try:
            bs = get_brain_settings()
            if bs.base_url:
                probes.append(_check_morphik(client, bs.base_url, bs.morphik_token))
            if bs.crawler_url:
                probes.append(_check_crawl4ai(client, bs.crawler_url, bs.crawl4ai_token))
        except Exception as exc:  # noqa: BLE001
            checks.append(_config_error("Brain", exc))

        checks.extend(await _gather_probes(probes))

    return checks
