from __future__ import annotations

from unittest.mock import patch

from kai.bots.waha.tts import (
    detect_kokoro_lang,
    parse_voice_map,
    resolve_kokoro_lang,
    resolve_kokoro_voice,
)


class TestResolveKokoroLang:
    def test_known_names(self) -> None:
        assert resolve_kokoro_lang("English") == "en-us"
        assert resolve_kokoro_lang("spanish") == "es"
        assert resolve_kokoro_lang("JAPANESE") == "ja"

    def test_empty_falls_back(self) -> None:
        assert resolve_kokoro_lang("") == "en-us"
        assert resolve_kokoro_lang("   ") == "en-us"

    def test_unknown_falls_back(self) -> None:
        assert resolve_kokoro_lang("klingon") == "en-us"


class TestDetectKokoroLang:
    def test_japanese_kana(self) -> None:
        assert detect_kokoro_lang("こんにちは、元気ですか?") == "ja"

    def test_mandarin_han(self) -> None:
        assert detect_kokoro_lang("你好,今天怎么样?") == "cmn"

    def test_hindi_devanagari(self) -> None:
        assert detect_kokoro_lang("नमस्ते, आप कैसे हैं?") == "hi"

    def test_spanish_stopwords(self) -> None:
        assert detect_kokoro_lang("Hola, ¿cómo estás? Que bueno verte.") == "es"

    def test_french_stopwords(self) -> None:
        assert detect_kokoro_lang("Bonjour, comment ça va? C'est très bien.") == "fr-fr"

    def test_italian_stopwords(self) -> None:
        assert detect_kokoro_lang("Ciao, come stai? Sono molto felice.") == "it"

    def test_portuguese_stopwords(self) -> None:
        assert detect_kokoro_lang("Oi, como você está? Isso é muito bom.") == "pt-br"

    def test_english_stopwords(self) -> None:
        assert detect_kokoro_lang("Hi there, how are you doing today?") == "en-us"

    def test_english_collapses_to_configured_variant(self) -> None:
        # British English can't be told from American via stopwords; an en-gb
        # bot keeps en-gb so its configured voice (bf_emma) is used.
        assert detect_kokoro_lang("Hi there, how are you doing today?", fallback="en-gb") == "en-gb"
        # And en-us stays en-us.
        assert detect_kokoro_lang("Hi there, how are you doing today?", fallback="en-us") == "en-us"

    def test_kanji_only_japanese_honors_configured_lang(self) -> None:
        # Terse kanji-only acknowledgments are ambiguous (ja vs cmn); a
        # Japanese-configured bot keeps ja so it uses the Japanese voice.
        assert detect_kokoro_lang("了解", fallback="ja") == "ja"
        # A Chinese-configured bot keeps cmn.
        assert detect_kokoro_lang("你好", fallback="cmn") == "cmn"
        # An English-configured bot receiving Han defaults to cmn (the more
        # common Han-only source) rather than an English voice on kanji.
        assert detect_kokoro_lang("你好", fallback="en-us") == "cmn"

    def test_inconclusive_latin_uses_fallback(self) -> None:
        assert detect_kokoro_lang("OK!", fallback="es") == "es"
        assert detect_kokoro_lang("...", fallback="en-us") == "en-us"

    def test_empty_uses_fallback(self) -> None:
        assert detect_kokoro_lang("", fallback="fr-fr") == "fr-fr"

    def test_unsupported_fallback_falls_back_to_text(self) -> None:
        # A fallback that Kokoro can't synthesize (e.g. a bot configured for
        # Russian) must not be silently guessed as English for inconclusive
        # Latin text/punctuation-only text — the caller should fall back to
        # a text reply instead of misattributing the voice.
        assert detect_kokoro_lang("...", fallback="ru") is None

    def test_cyrillic_unsupported(self) -> None:
        assert detect_kokoro_lang("Привет, как дела?") is None

    def test_arabic_unsupported(self) -> None:
        assert detect_kokoro_lang("مرحبا، كيف حالك؟") is None

    def test_korean_unsupported(self) -> None:
        assert detect_kokoro_lang("안녕하세요, 어떻게 지내세요?") is None


class TestParseVoiceMap:
    def test_empty(self) -> None:
        assert parse_voice_map("") == {}

    def test_simple(self) -> None:
        assert parse_voice_map("es=ef_dora,fr-fr=ff_siwis") == {
            "es": "ef_dora",
            "fr-fr": "ff_siwis",
        }

    def test_skips_malformed(self) -> None:
        assert parse_voice_map("es=ef_dora,bad,=x, ,en-us=af_heart") == {
            "es": "ef_dora",
            "en-us": "af_heart",
        }


class TestResolveKokoroVoice:
    def test_override_wins(self) -> None:
        assert resolve_kokoro_voice("es", overrides={"es": "em_alex"}) == "em_alex"

    def test_builtin_default(self) -> None:
        assert resolve_kokoro_voice("fr-fr") == "ff_siwis"

    def test_configured_default_voice_via_seeded_override(self) -> None:
        # The operator's chosen primary voice for en-us is pre-seeded into the
        # override map at startup, so it wins over the built-in af_heart.
        assert resolve_kokoro_voice("en-us", overrides={"en-us": "af_bella"}) == "af_bella"

    def test_unsupported_returns_none(self) -> None:
        assert resolve_kokoro_voice("ru") is None


class TestSynthesizePlumbsVoiceAndLang:
    def test_detect_then_synthesize_uses_spanish_voice(self) -> None:
        text = "Hola, ¿cómo estás? Que bueno verte."
        lang = detect_kokoro_lang(text, fallback="en-us")
        assert lang == "es"
        voice = resolve_kokoro_voice(lang)
        assert voice == "ef_dora"
        with patch("kai.bots.waha.tts.httpx.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.content = b"WAV"
            from kai.bots.waha.tts import synthesize

            synthesize(text, voice=voice, lang=lang)
            kwargs = mock_post.call_args.kwargs["json"]
            assert kwargs["voice"] == "ef_dora"
            assert kwargs["lang"] == "es"
