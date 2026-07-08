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
from dataclasses import dataclass, field
from typing import Any

import httpx

from kai.brain.config import BrainSettings, get_brain_settings

logger = logging.getLogger(__name__)


@dataclass
class CrawlLinks:
    """The internal/external links discovered on a crawled page."""

    internal: list[str] = field(default_factory=list)
    external: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, links: dict[str, Any] | None) -> CrawlLinks:
        if not links:
            return cls()
        internal: list[str] = []
        external: list[str] = []
        for entry in links.get("internal", []) or []:
            href = entry.get("href") if isinstance(entry, dict) else entry
            if isinstance(href, str):
                internal.append(href)
        for entry in links.get("external", []) or []:
            href = entry.get("href") if isinstance(entry, dict) else entry
            if isinstance(href, str):
                external.append(href)
        return cls(internal=internal, external=external)


@dataclass
class MarkdownResult:
    """The markdown dict returned by POST /crawl.

    NOTE: ``/md`` returns markdown as a *string* (handled by
    ``extract_markdown``); ``/crawl`` returns it as a *dict* with these
    keys. ``fit_markdown`` is empty via ``/crawl`` (the fit filter
    only runs on the ``/md`` path), so consumers of ``crawl()`` should read
    ``raw_markdown``.
    """

    raw_markdown: str = ""
    markdown_with_citations: str = ""
    references_markdown: str = ""
    fit_markdown: str = ""

    @classmethod
    def from_dict(cls, md: Any) -> MarkdownResult:
        """Accept either the dict (from /crawl) or a string (defensive)."""
        if isinstance(md, dict):
            return cls(
                raw_markdown=md.get("raw_markdown", "") or "",
                markdown_with_citations=md.get("markdown_with_citations", "") or "",
                references_markdown=md.get("references_markdown", "") or "",
                fit_markdown=md.get("fit_markdown", "") or "",
            )
        if isinstance(md, str):
            return cls(raw_markdown=md)
        return cls()


@dataclass
class CrawlPage:
    """The full result of a POST /crawl single-page fetch.

    ``markdown`` is the MarkdownResult (dict-extracted); ``links`` is the
    discovered internal/external links (the BFS frontier for whole-site
    crawl). ``success`` is the per-page fetch flag.
    """

    url: str
    success: bool
    markdown: MarkdownResult
    links: CrawlLinks
    error_message: str | None = None

    @classmethod
    def from_result(cls, result: dict[str, Any]) -> CrawlPage:
        return cls(
            url=result.get("url", ""),
            success=bool(result.get("success")),
            markdown=MarkdownResult.from_dict(result.get("markdown")),
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
            timeout=60.0,  # headless Chromium render is ~2-5s per page; allow slack for slow sites
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
        if isinstance(md, dict):  # defensive — /md is documented as string
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
