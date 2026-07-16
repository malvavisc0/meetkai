"""Crawl4aiClient — typed async wrapper over the crawl4ai HTTP API.

Two methods, both POST with ``Authorization: Bearer``:
- ``extract_markdown(url)`` → ``POST /md`` (returns markdown as a string)
- ``crawl(url)`` → ``POST /crawl`` (returns full result incl. internal links)

The whole-site BFS is **not** in this client: crawl4ai 0.9.0 rejects
``deep_crawl_strategy`` over HTTP for security, so kai orchestrates
the BFS in ``BrainsService.ingest_url`` using ``crawl()``'s returned links.
This client is the thin HTTP layer the cockpit BFS loop calls.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from kai.brain.config import BrainSettings, get_brain_settings

logger = logging.getLogger(__name__)


class CrawlLinks(BaseModel):
    """The internal/external links discovered on a crawled page."""

    model_config = ConfigDict(frozen=True)

    internal: list[str] = Field(default_factory=list)
    external: list[str] = Field(default_factory=list)

    @classmethod
    def from_dict(cls, links: dict[str, Any] | None) -> CrawlLinks:
        if not links:
            return cls()

        def _hrefs(entries: list[Any]) -> list[str]:
            out: list[str] = []
            for entry in entries:
                href = entry.get("href") if isinstance(entry, dict) else entry
                if isinstance(href, str):
                    out.append(href)
            return out

        return cls(
            internal=_hrefs(links.get("internal") or []),
            external=_hrefs(links.get("external") or []),
        )


class MarkdownResult(BaseModel):
    """The markdown dict returned by POST /crawl.

    NOTE: ``/md`` returns markdown as a *string* (handled by
    ``extract_markdown``); ``/crawl`` returns it as a *dict* with these
    keys. ``fit_markdown`` is empty via ``/crawl`` (the fit filter
    only runs on the ``/md`` path), so consumers of ``crawl()`` should read
    ``raw_markdown``.
    """

    model_config = ConfigDict(frozen=True)

    raw_markdown: str = ""
    markdown_with_citations: str = ""
    references_markdown: str = ""
    fit_markdown: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce_source(cls, data: Any) -> Any:
        """Accept the /crawl dict shape, or a bare string (defensive)."""
        if isinstance(data, str):
            return {"raw_markdown": data}
        if data is None:
            return {}
        return data


class CrawlPage(BaseModel):
    """The full result of a POST /crawl single-page fetch.

    ``markdown`` is the MarkdownResult (dict-extracted); ``links`` is the
    discovered internal/external links (the BFS frontier for whole-site
    crawl). ``success`` is the per-page fetch flag.
    """

    model_config = ConfigDict(frozen=True)

    url: str = ""
    success: bool = False
    markdown: MarkdownResult = Field(default_factory=MarkdownResult)
    links: CrawlLinks = Field(default_factory=CrawlLinks)
    error_message: str | None = None

    @classmethod
    def from_result(cls, result: dict[str, Any]) -> CrawlPage:
        return cls(
            url=result.get("url", ""),
            success=bool(result.get("success")),
            markdown=MarkdownResult.model_validate(result.get("markdown")),
            links=CrawlLinks.from_dict(result.get("links")),
            error_message=result.get("error_message"),
        )


class Crawl4aiClient:
    """Async HTTP client for crawl4ai v0.9.0.

    Mirrors the LightRagClient / WahaClient pattern. The bearer token
    (``KAI_BRAIN_CRAWL4AI_TOKEN``) is set on construction. No LLM key is
    configured on the container — the Brain never uses ``f="llm"``.
    """

    def __init__(self, settings: BrainSettings | None = None) -> None:
        self.settings = settings or get_brain_settings()
        self.base_url = self.settings.crawler_url.rstrip("/")
        headers: dict[str, str] = {"Authorization": f"Bearer {self.settings.crawl4ai_token}"}
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=60.0,  # pages take ~2-5s to render; allow slack for slow sites
            headers=headers,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def extract_markdown(self, *, url: str, strategy: str = "fit") -> str:
        """POST /md — single-page markdown extraction.

        ``strategy`` is the ``f`` filter: "fit" (content-filtered, default),
        "raw" (full page), "bm25" / "llm" (query-aware; "llm" needs an LLM
        key on the container, which the Brain never configures). Returns the
        markdown as a **string** (unlike ``crawl()`` which returns a dict).
        """
        resp = await self._client.post(
            "/md",
            json={"url": url, "f": strategy},
        )
        resp.raise_for_status()
        data = resp.json()
        # /md returns {"markdown": "<string>", "success": true, ...}
        md = data.get("markdown", "")
        if isinstance(md, dict):  # /md should return a string, but handle unexpected dict
            md = md.get("raw_markdown", "") or ""
        return md if isinstance(md, str) else ""

    async def crawl(self, *, url: str, exclude_external_links: bool = True) -> CrawlPage:
        """POST /crawl — single-page fetch returning markdown + discovered links.

        This is the method ``BrainsService.ingest_url`` uses for whole-site
        crawl: the returned ``links.internal`` drives the BFS loop.
        ``exclude_external_links=True`` keeps the link list same-domain only,
        which is what the BFS scope wants.

        NOTE: ``deep_crawl_strategy`` is intentionally NOT accepted here —
        crawl4ai 0.9.0 rejects it over HTTP. Whole-site crawl is
        kai-orchestrated, not server-side.
        """
        resp = await self._client.post(
            "/crawl",
            json={
                "urls": [url],
                "crawler_config": {
                    "cache_mode": "bypass",
                    "exclude_external_links": exclude_external_links,
                },
            },
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results") or []
        if not results:
            # No results list (unexpected for /crawl) — synthesize a failure.
            return CrawlPage(
                url=url,
                success=False,
                markdown=MarkdownResult(),
                links=CrawlLinks(),
                error_message="crawl returned no results",
            )
        return CrawlPage.from_result(results[0])
