from __future__ import annotations

import logging
import re
import unicodedata

import httpx

logger = logging.getLogger(__name__)

# Map common language names (as given via --language / config.json) to Kokoro
# language codes accepted by kokoro_onnx.Kokoro.create(lang=...). Keys are
# matched case-insensitively. Unknown names fall back to "en-us".
#
# NOTE: only languages Kokoro v1.0 actually ships voices for are listed here
# (see _DEFAULT_VOICE_BY_LANG). Russian/Arabic/Korean were previously listed
# but have no voices in v1.0, so synthesizing them was silently broken.
_LANGUAGE_NAME_TO_KOKORO_LANG: dict[str, str] = {
    "english": "en-us",
    "british english": "en-gb",
    "spanish": "es",
    "french": "fr-fr",
    "italian": "it",
    "portuguese": "pt-br",
    "brazilian portuguese": "pt-br",
    "chinese": "cmn",
    "mandarin": "cmn",
    "japanese": "ja",
    "hindi": "hi",
}

# Kokoro v1.0 supported languages and their best default voice (highest-graded
# female voice per language, from huggingface.co/hexgrad/Kokoro-82M VOICES.md).
# A reply detected as one of these languages is synthesized with the matching
# voice unless the operator overrides it via KAI_WAHA_KOKORO_VOICE_MAP.
_DEFAULT_VOICE_BY_LANG: dict[str, str] = {
    "en-us": "af_heart",
    "en-gb": "bf_emma",
    "ja": "jf_alpha",
    "cmn": "zf_xiaoxiao",
    "es": "ef_dora",
    "fr-fr": "ff_siwis",
    "hi": "hf_alpha",
    "it": "if_sara",
    "pt-br": "pf_dora",
}

# Public, stable list of Kokoro v1.0 supported lang codes — used by callers
# (e.g. the cockpit settings UI) that need to show operators which lang
# codes are valid for KAI_WAHA_KOKORO_VOICE_MAP / kokoro_voice_map entries.
SUPPORTED_KOKORO_LANGS: tuple[str, ...] = tuple(_DEFAULT_VOICE_BY_LANG.keys())

# One canonical display name per supported lang code, for surfacing to
# humans/the agent (see SUPPORTED_KOKORO_LANGUAGE_NAMES below). Picks the more
# common of any synonyms in _LANGUAGE_NAME_TO_KOKORO_LANG (e.g. "Chinese"
# over "Mandarin", "English" over "British English" — the en-us/en-gb variant
# distinction only matters for voice *selection* via resolve_kokoro_voice,
# not for this yes/no capability list).
_CANONICAL_LANG_NAME_BY_CODE: dict[str, str] = {
    "en-us": "English",
    "en-gb": "English",
    "es": "Spanish",
    "fr-fr": "French",
    "it": "Italian",
    "pt-br": "Portuguese",
    "cmn": "Chinese",
    "ja": "Japanese",
    "hi": "Hindi",
}

# Human-readable names for SUPPORTED_KOKORO_LANGS, deduplicated and derived
# from _CANONICAL_LANG_NAME_BY_CODE so it can't silently drift out of sync
# with _DEFAULT_VOICE_BY_LANG — adding/removing a lang code there without a
# matching entry here raises KeyError immediately instead of the agent
# silently getting a stale supported-language list. Used to tell the agent
# which languages voice notes actually work for — see
# WahaBot._tts_capability_note — so it doesn't promise/attempt a voice note
# in a language Kokoro has no voice for (e.g. German) and can tell the user
# in text instead.
SUPPORTED_KOKORO_LANGUAGE_NAMES: tuple[str, ...] = tuple(
    dict.fromkeys(_CANONICAL_LANG_NAME_BY_CODE[code] for code in SUPPORTED_KOKORO_LANGS)
)

# Unicode letter ranges that count as "Latin" (ASCII + accented Latin in the
# Latin-1 Supplement / Latin Extended blocks). Used to distinguish accented
# Latin text (Spanish/French/Portuguese) from genuinely unsupported scripts
# like Cyrillic, Arabic, or Hangul.
_LATIN_RANGES: tuple[tuple[int, int], ...] = (
    (0x0041, 0x005B),  # A-Z
    (0x0061, 0x007B),  # a-z
    (0x00C0, 0x0180),  # Latin-1 Supplement letters + Latin Extended-A
    (0x0180, 0x0250),  # Latin Extended-B
    (0x1E00, 0x1F00),  # Latin Extended Additional
)

# Latin-script detection by common-word frequency. Tokens are accent-folded
# (``cómo`` → ``como``) and lowercased so diacritics don't defeat matching.
# Ordered: the first language with the highest score wins (ties resolve to the
# earlier entry), so the discriminative unique words below are what actually
# separate close relatives (Spanish vs Portuguese).
_LATIN_STOPWORDS: dict[str, frozenset[str]] = {
    "pt-br": frozenset(
        {
            "que",
            "nao",
            "uma",
            "voce",
            "com",
            "para",
            "isso",
            "esta",
            "estas",
            "mais",
            "como",
            "tem",
            "mas",
            "dos",
            "das",
            "seu",
            "sua",
            "bom",
            "boa",
            "oi",
            "ola",
            "sim",
            "obrigado",
            "onde",
            "quem",
            "qual",
            "muito",
            "tudo",
            "bem",
            "sou",
            "estou",
        }
    ),
    "es": frozenset(
        {
            "que",
            "de",
            "no",
            "una",
            "con",
            "para",
            "eso",
            "esta",
            "estas",
            "mas",
            "como",
            "pero",
            "los",
            "las",
            "por",
            "sus",
            "muy",
            "hola",
            "bueno",
            "buena",
            "soy",
            "eres",
            "gracias",
            "donde",
            "quien",
            "cual",
            "aqui",
            "ahora",
            "si",
            "todo",
            "bien",
        }
    ),
    "fr-fr": frozenset(
        {
            "que",
            "de",
            "ne",
            "une",
            "avec",
            "pour",
            "est",
            "plus",
            "comme",
            "mais",
            "les",
            "des",
            "pas",
            "vous",
            "nous",
            "tres",
            "bonjour",
            "salut",
            "oui",
            "merci",
            "ou",
            "comment",
            "vas",
            "suis",
            "es",
            "ca",
        }
    ),
    "it": frozenset(
        {
            "che",
            "di",
            "non",
            "una",
            "con",
            "per",
            "piu",
            "come",
            "sono",
            "gli",
            "suo",
            "sua",
            "anche",
            "questo",
            "quello",
            "ciao",
            "stai",
            "grazie",
            "si",
            "perche",
            "cosa",
            "bene",
            "tutto",
            "molto",
        }
    ),
    "en-us": frozenset(
        {
            "the",
            "and",
            "that",
            "you",
            "it",
            "is",
            "to",
            "of",
            "a",
            "in",
            "im",
            "dont",
            "cant",
            "youre",
            "thats",
            "hi",
            "hello",
            "yes",
            "no",
            "thanks",
            "how",
            "are",
            "do",
            "what",
            "was",
            "its",
        }
    ),
    # NOTE: British English (en-gb) has no separate stopword set — short
    # replies can't reliably distinguish it from American English. When the
    # stopword winner is an English variant, detect_kokoro_lang collapses to
    # the configured fallback if it is itself English (see _ENGLISH_VARIANTS),
    # so a British-configured bot keeps its en-gb voice.
}

# Kokoro's two English variants. detect_kokoro_lang returns the configured
# fallback for any English-detection winner so an en-gb bot isn't forced to
# en-us (and vice-versa).
_ENGLISH_VARIANTS: frozenset[str] = frozenset({"en-us", "en-gb"})

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _fold_accents(s: str) -> str:
    """Lowercase and strip combining diacritics: ``cómo`` → ``como``."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c)
    )


def resolve_kokoro_lang(language: str) -> str | None:
    """Resolve a language name (e.g. "Spanish") to a Kokoro lang code (e.g. "es").

    Returns ``None`` when *language* is empty or isn't one Kokoro v1.0 ships
    voices for (e.g. "German", "Dutch", "Klingon"). Coercing an unsupported
    language to "en-us" here used to defeat the unsupported-language guard
    in :func:`detect_kokoro_lang`/:func:`resolve_kokoro_voice` below: the
    bot's own configured language would be silently treated as English,
    so replies got synthesized with English phonemization and whatever
    voice the operator had (wrongly) picked for that language — audibly
    "speaking German with an English accent". Callers must fall back to a
    text reply when this returns ``None``.
    """
    if not language:
        return None
    lang = language.strip().lower()
    if not lang:
        return None
    return _LANGUAGE_NAME_TO_KOKORO_LANG.get(lang)


def _is_supported(lang: str) -> bool:
    return lang in _DEFAULT_VOICE_BY_LANG


def detect_kokoro_lang(text: str, *, fallback: str | None = "en-us") -> str | None:
    """Detect which Kokoro-supported language *text* is written in.

    Returns a Kokoro lang code from the v1.0 supported set. Returns ``None``
    when the script is not supported by Kokoro v1.0 (Cyrillic, Arabic, Korean,
    Thai, ...); callers should fall back to a text reply in that case. For
    inconclusive Latin text (no common words matched), returns *fallback* when
    it is itself a supported language. If *fallback* is ``None`` or not a
    Kokoro-supported language (e.g. a bot configured for German, Dutch,
    Polish, ...), inconclusive Latin text also returns ``None`` rather than
    guessing an unrelated voice (e.g. English) for text Kokoro was never
    asked to speak.
    """
    fallback_supported = fallback is not None and _is_supported(fallback)
    fb = fallback if fallback_supported else "en-us"

    # 1) Non-Latin script detection.
    has_kana = any(0x3040 <= ord(ch) < 0x3100 for ch in text)  # Hiragana/Katakana
    if has_kana:
        return "ja"
    if any(0x0900 <= ord(ch) < 0x0980 for ch in text):  # Devanagari
        return "hi"
    has_han = any(0x4E00 <= ord(ch) < 0x9FFF for ch in text)  # CJK Unified
    if has_han:
        # Han without kana is ambiguous (Japanese kanji vs Mandarin). Honor
        # the configured language when it's one of the two; otherwise default
        # to Mandarin, the more common Han-only source language.
        return fb if fb in ("ja", "cmn") else "cmn"
    # Any non-Latin, non-ASCII lettering outside the supported script blocks
    # above means Kokoro v1.0 can't synthesize it (Cyrillic, Arabic, Hangul,
    # Thai, ...). Accented Latin characters (ó, é, ç, ...) are *not* flagged
    # here so Spanish/French/Portuguese detection still works.
    for ch in text:
        if ch.isalpha() and not ch.isascii():
            o = ord(ch)
            if not any(lo <= o < hi for lo, hi in _LATIN_RANGES):
                return None

    # 2) Latin script: stopword frequency tally (accents folded).
    tokens = {_fold_accents(t) for t in _WORD_RE.findall(text)}
    if not tokens:
        return fb if fallback_supported else None
    best: str | None = None
    best_score = 0
    for lang, words in _LATIN_STOPWORDS.items():
        score = len(tokens & words)
        if score > best_score:
            best_score = score
            best = lang
    if best is not None and best_score > 0:
        # British vs American English can't be told apart from short replies;
        # collapse any English winner to the configured variant when one is
        # configured, so an en-gb bot keeps its en-gb voice.
        if best in _ENGLISH_VARIANTS and fb in _ENGLISH_VARIANTS:
            return fb
        return best
    # Inconclusive Latin text: only guess the configured fallback when it is
    # itself a Kokoro-supported language. Otherwise (e.g. a bot configured
    # for German/Dutch/Polish/...), don't misattribute the text to an
    # unrelated voice like English — signal the caller to use text instead.
    return fb if fallback_supported else None


def parse_voice_map(raw: str) -> dict[str, str]:
    """Parse a 'lang=voice,lang=voice' override string into a dict.

    Malformed entries (missing '=', empty lang, or empty voice) are dropped
    and logged so a typo in KAI_WAHA_KOKORO_VOICE_MAP is visible at startup
    rather than silently ignored.
    """
    out: dict[str, str] = {}
    if not raw:
        return out
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            logger.warning("Ignoring malformed Kokoro voice map entry (missing '='): %r", part)
            continue
        lang, voice = part.split("=", 1)
        lang = lang.strip().lower()
        voice = voice.strip()
        if not lang or not voice:
            logger.warning("Ignoring malformed Kokoro voice map entry: %r", part)
            continue
        out[lang] = voice
    return out


def resolve_kokoro_voice(
    lang: str,
    *,
    overrides: dict[str, str] | None = None,
) -> str | None:
    """Pick a Kokoro voice for *lang*.

    Precedence: operator override map > built-in default-voice table. Returns
    ``None`` if *lang* is not a Kokoro v1.0 supported language, signaling the
    caller to fall back to a text reply. The operator's configured primary
    voice is expected to be pre-seeded into *overrides* for the configured
    language (see ``WahaBot`` startup), so a reply in the bot's own language
    keeps the operator's chosen voice.
    """
    if not _is_supported(lang):
        return None
    if overrides and lang in overrides:
        return overrides[lang]
    return _DEFAULT_VOICE_BY_LANG[lang]


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
