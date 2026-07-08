from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

# Map common language names (as given via --language / config.json) to Kokoro
# language codes accepted by kokoro_onnx.Kokoro.create(lang=...). Keys are
# matched case-insensitively. Unknown names fall back to "en-us".
_LANGUAGE_NAME_TO_KOKORO_LANG: dict[str, str] = {
    "english": "en-us",
    "spanish": "es",
    "french": "fr-fr",
    "german": "de",
    "italian": "it",
    "portuguese": "pt-br",
    "chinese": "cmn",
    "mandarin": "cmn",
    "japanese": "ja",
    "korean": "ko",
    "hindi": "hi",
    "arabic": "ar",
    "russian": "ru",
}


def resolve_kokoro_lang(language: str) -> str:
    """Resolve a language name (e.g. "Spanish") to a Kokoro lang code (e.g. "es").

    Returns "en-us" for empty/unknown input so synthesis never silently fails.
    """
    if not language:
        return "en-us"
    lang = language.strip().lower()
    if not lang:
        return "en-us"
    return _LANGUAGE_NAME_TO_KOKORO_LANG.get(lang, "en-us")


def check_kokoro_available(
    host: str = "127.0.0.1",
    port: int = 8788,
) -> tuple[bool, str]:
    """Probe the kokoro server's /health endpoint.

    Returns ``(True, "")`` when the server is healthy, or ``(False, reason)``
    with a human-readable explanation otherwise.
    """
    url = f"http://{host}:{port}/health"
    try:
        resp = httpx.get(url, timeout=5)
        if resp.status_code == 200:
            return True, ""
        return False, f"kokoro server returned {resp.status_code}"
    except httpx.ConnectError:
        return False, f"kokoro server not reachable at {url}"
    except (httpx.ReadTimeout, httpx.HTTPError) as exc:
        return False, f"kokoro server health check failed: {exc}"


def synthesize(
    text: str,
    host: str = "127.0.0.1",
    port: int = 8788,
    voice: str = "af_heart",
    lang: str = "en-us",
    speed: float = 1.0,
) -> bytes | None:
    """Synthesize text to WAV bytes via the kokoro server.

    POSTs JSON to the server's ``/synthesize`` endpoint.
    Returns ``None`` on any failure (timeout, server error, network error)
    so callers can fall back to a text reply.
    """
    url = f"http://{host}:{port}/synthesize"
    try:
        resp = httpx.post(
            url,
            json={"text": text, "voice": voice, "lang": lang, "speed": speed},
            timeout=60,
        )
        if resp.status_code == 200:
            return resp.content
        logger.warning("kokoro server returned %d: %s", resp.status_code, resp.text[:200])
        return None
    except (httpx.TimeoutException, httpx.HTTPError) as exc:
        logger.warning("kokoro synthesis failed: %s", exc)
        return None
