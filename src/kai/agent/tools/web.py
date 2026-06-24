import logging
import re

import httpx
from ddgs import DDGS
from markdownify import markdownify as md

logger = logging.getLogger(__name__)

_WEBPAGE_MAX_CHARS = 8000

_BOILERPLATE_TAG_RE = {
    "script": re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL),
    "style": re.compile(r"<style\b[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL),
    "noscript": re.compile(r"<noscript\b[^>]*>.*?</noscript>", re.IGNORECASE | re.DOTALL),
    "svg": re.compile(r"<svg\b[^>]*>.*?</svg>", re.IGNORECASE | re.DOTALL),
    "template": re.compile(r"<template\b[^>]*>.*?</template>", re.IGNORECASE | re.DOTALL),
    "nav": re.compile(r"<nav\b[^>]*>.*?</nav>", re.IGNORECASE | re.DOTALL),
    "header": re.compile(r"<header\b[^>]*>.*?</header>", re.IGNORECASE | re.DOTALL),
    "footer": re.compile(r"<footer\b[^>]*>.*?</footer>", re.IGNORECASE | re.DOTALL),
    "aside": re.compile(r"<aside\b[^>]*>.*?</aside>", re.IGNORECASE | re.DOTALL),
    "form": re.compile(r"<form\b[^>]*>.*?</form>", re.IGNORECASE | re.DOTALL),
}

_BODY_RE = re.compile(r"<body\b[^>]*>(.*?)</body>", re.IGNORECASE | re.DOTALL)


def _web_search(query: str, max_results: int = 15) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    try:
        raw_results = DDGS().text(query, max_results=max_results)
    except Exception:
        logger.warning("web_search failed for query %r", query, exc_info=True)
        return results
    for result in raw_results:
        if isinstance(result, dict):
            results.append(
                {
                    "title": str(result.get("title", "")),
                    "url": str(result.get("url") or result.get("href", "")),
                    "snippet": str(result.get("snippet") or result.get("body", "")),
                }
            )
        else:
            results.append(
                {
                    "title": str(getattr(result, "title", "")),
                    "url": str(getattr(result, "url", "") or getattr(result, "href", "")),
                    "snippet": str(getattr(result, "snippet", "") or getattr(result, "body", "")),
                }
            )

    return results


def _strip_boilerplate(html: str) -> str:
    for pattern in _BOILERPLATE_TAG_RE.values():
        html = pattern.sub("", html)
    match = _BODY_RE.search(html)
    return match.group(1) if match else html


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text.rfind(" ", 0, max_chars)
    if cut == -1:
        cut = max_chars
    return text[:cut].rstrip() + "\n\n[content truncated]"


def _get_webpage_content(
    url: str, timeout: float = 20.0, max_chars: int = _WEBPAGE_MAX_CHARS
) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
    }

    try:
        with httpx.Client(
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            cleaned = _strip_boilerplate(response.text)
            return _truncate(md(cleaned), max_chars)
    except httpx.HTTPStatusError as exc:
        logger.warning("failed to fetch webpage %s: %s", url, exc)
        return f"Error: HTTP {exc.response.status_code} fetching {url}"
    except httpx.TimeoutException:
        logger.warning("timed out fetching webpage %s", url)
        return f"Error: timed out after {timeout}s fetching {url}"
    except (httpx.HTTPError, httpx.RequestError) as exc:
        logger.warning("failed to fetch webpage %s: %s", url, exc)
        return f"Error: could not fetch {url} ({type(exc).__name__})"
