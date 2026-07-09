"""Aggregated service-health probes for the cockpit.

Returns a list of :class:`HealthCheck` for the external dependencies a
deployment relies on (beyond the bot process itself, whose status is
already surfaced on the deployment page): the database, WhatsApp/WAHA,
the LLM provider, and the optional media + brain services.

Probes are bounded by short timeouts and run concurrently (including the
DB ping), so a single unreachable service never stalls the page.

Only services that are *expected* to run appear in the result — a service
disabled by config (e.g. ``kokoro_enabled=False``) is omitted rather than
flagged as down.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from kai.bots.waha.config import get_waha_settings
from kai.brain.config import get_brain_settings
from kai.cockpit.db import engine
from kai.config.settings import get_settings

_TIMEOUT = 3.0

_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    """Return a shared AsyncClient so TCP/TLS connections are pooled across probes."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _client


@dataclass
class HealthCheck:
    label: str
    ok: bool = False
    detail: str = ""


async def _probe(url: str, *, headers: dict[str, str] | None = None) -> tuple[bool, str]:
    """Single-shot GET; (ok, detail). 200 only."""
    client = await _get_client()
    try:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            return True, "responding"
        return False, f"HTTP {resp.status_code}"
    except httpx.ConnectError:
        return False, "not reachable"
    except (httpx.ReadTimeout, httpx.HTTPError) as exc:
        return False, type(exc).__name__


def _db_ping() -> tuple[bool, str]:
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        return True, "ok"
    except Exception as exc:  # noqa: BLE001 - surfaced as detail, not raised
        return False, f"{type(exc).__name__}"


async def _check_db() -> HealthCheck:
    ok, detail = await asyncio.to_thread(_db_ping)
    return HealthCheck(label="Database", ok=ok, detail=detail)


async def _check_llm() -> HealthCheck:
    settings = get_settings()
    hc = HealthCheck(label="LLM API")
    if not settings.llm_api_key:
        hc.detail = "no API key configured"
        return hc
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    ok, detail = await _probe(f"{settings.llm_api_base.rstrip('/')}/models", headers=headers)
    hc.ok = ok
    hc.detail = detail
    return hc


async def _check_whisper(host: str, port: int) -> HealthCheck:
    ok, detail = await _probe(f"http://{host}:{port}/health")
    return HealthCheck(label="Speech to Text Service", ok=ok, detail=detail)


async def _check_kokoro(host: str, port: int) -> HealthCheck:
    ok, detail = await _probe(f"http://{host}:{port}/health")
    return HealthCheck(label="Text to Speech Service", ok=ok, detail=detail)


async def _check_lightrag(base_url: str, api_key: str) -> HealthCheck:
    headers = {"X-API-Key": api_key} if api_key else None
    ok, detail = await _probe(f"{base_url.rstrip('/')}/health", headers=headers)
    return HealthCheck(label="RAG Server", ok=ok, detail=detail)


async def _check_crawl4ai(crawler_url: str, token: str) -> HealthCheck:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    ok, detail = await _probe(f"{crawler_url.rstrip('/')}/health", headers=headers)
    return HealthCheck(label="Crawler", ok=ok, detail=detail)


def _config_error(label: str, exc: Exception) -> HealthCheck:
    return HealthCheck(label=label, ok=False, detail=f"config error: {type(exc).__name__}")


async def check_service_health(
    *,
    whatsapp_status: str | None,
) -> list[HealthCheck]:
    """Probe the external dependencies the deployment relies on.

    ``whatsapp_status`` is the cached ``Connection.status`` value
    (``"connected"`` / ``"connecting"`` / ``"disconnected"``) or None when
    no WhatsApp connection exists. The bot process itself is intentionally
    not probed here — its status is already shown on the deployment page.
    """
    checks: list[HealthCheck] = []

    wa_ok = whatsapp_status == "connected"
    checks.append(
        HealthCheck(
            label="WhatsApp API",
            ok=wa_ok,
            detail=whatsapp_status or "not configured",
        )
    )

    # --- Concurrent probes (DB + network) ---
    probes: list = [_check_db(), _check_llm()]

    try:
        waha = get_waha_settings()
        if waha.whisper_server_mode:
            probes.append(_check_whisper(waha.whisper_server_host, waha.whisper_server_port))
        if waha.kokoro_enabled:
            probes.append(_check_kokoro(waha.kokoro_server_host, waha.kokoro_server_port))
    except Exception as exc:  # noqa: BLE001 - config error shouldn't kill the card
        checks.append(_config_error("Media services", exc))

    try:
        bs = get_brain_settings()
        if bs.base_url:
            probes.append(_check_lightrag(bs.base_url, bs.lightrag_api_key))
        if bs.crawler_url:
            probes.append(_check_crawl4ai(bs.crawler_url, bs.crawl4ai_token))
    except Exception as exc:  # noqa: BLE001
        checks.append(_config_error("Brain", exc))

    results = await asyncio.gather(*probes, return_exceptions=True)
    for r in results:
        if isinstance(r, HealthCheck):
            checks.append(r)
        else:
            checks.append(HealthCheck(label="Probe", detail=f"{type(r).__name__}"))

    return checks
