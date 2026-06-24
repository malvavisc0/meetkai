import base64

from kai.bots.waha.media import MediaType, extract_media


def _image_url_msg(url: str = "http://localhost:3000/api/files/default/img.jpg") -> dict:
    return {
        "type": "image",
        "mimetype": "image/jpeg",
        "filename": "img.jpg",
        "mediaUrl": url,
    }


def _image_base64_msg(data: bytes = b"\x89PNG\r\n") -> dict:
    return {
        "type": "image",
        "mimetype": "image/png",
        "filename": "pic.png",
        "data": base64.b64encode(data).decode(),
    }


def _voice_url_msg(url: str = "http://localhost:3000/api/files/default/voice.oga") -> dict:
    return {
        "type": "ptt",
        "mimetype": "audio/ogg; codecs=opus",
        "filename": "voice.oga",
        "mediaUrl": url,
    }


def _voice_base64_msg(data: bytes = b"fake-opus-bytes") -> dict:
    return {
        "type": "ptt",
        "mimetype": "audio/ogg; codecs=opus",
        "filename": "voice.oga",
        "data": base64.b64encode(data).decode(),
    }


def _audio_url_msg() -> dict:
    return {
        "type": "audio",
        "mimetype": "audio/mpeg",
        "filename": "song.mp3",
        "mediaUrl": "http://localhost:3000/api/files/default/song.mp3",
    }


def _text_msg() -> dict:
    return {"type": "chat", "body": "hello"}


def _unknown_type_msg() -> dict:
    return {"type": "sticker", "mediaUrl": "http://localhost:3000/api/files/default/sticker.webp"}


class TestExtractImage:
    def test_image_from_url(self):
        result = extract_media(_image_url_msg())
        assert result is not None
        assert result.type is MediaType.IMAGE
        assert result.url == "http://localhost:3000/api/files/default/img.jpg"
        assert result.data is None
        assert result.mime_type == "image/jpeg"
        assert result.filename == "img.jpg"

    def test_image_from_base64(self):
        raw = b"\x89PNG\r\n"
        result = extract_media(_image_base64_msg(raw))
        assert result is not None
        assert result.type is MediaType.IMAGE
        assert result.data == raw
        assert result.url is None

    def test_image_base64_invalid_returns_none(self):
        msg = {"type": "image", "mimetype": "image/jpeg", "data": "!!!not-base64!!!"}
        result = extract_media(msg)
        assert result is None


class TestExtractVoice:
    def test_voice_from_url(self):
        result = extract_media(_voice_url_msg())
        assert result is not None
        assert result.type is MediaType.VOICE
        assert result.url == "http://localhost:3000/api/files/default/voice.oga"
        assert result.data is None

    def test_voice_from_base64(self):
        raw = b"fake-opus-bytes"
        result = extract_media(_voice_base64_msg(raw))
        assert result is not None
        assert result.type is MediaType.VOICE
        assert result.data == raw
        assert result.url is None


class TestExtractAudio:
    def test_audio_from_url(self):
        result = extract_media(_audio_url_msg())
        assert result is not None
        assert result.type is MediaType.AUDIO


class TestExtractUnknown:
    def test_text_message_returns_none(self):
        assert extract_media(_text_msg()) is None

    def test_unknown_type_returns_none(self):
        assert extract_media(_unknown_type_msg()) is None

    def test_sticker_returns_none(self):
        assert extract_media({"type": "sticker"}) is None


class TestExtractMediaEdgeCases:
    def test_no_data_no_url_returns_none(self):
        msg = {"type": "image", "mimetype": "image/jpeg"}
        assert extract_media(msg) is None

    def test_empty_data_field_returns_none(self):
        msg = {"type": "image", "data": ""}
        assert extract_media(msg) is None

    def test_url_only_if_data_absent(self):
        msg = {"type": "image", "mediaUrl": "http://example.com/img.jpg", "mimetype": "image/jpeg"}
        result = extract_media(msg)
        assert result is not None
        assert result.url == "http://example.com/img.jpg"

    def test_data_takes_precedence_over_url(self):
        raw = b"\x89PNG"
        msg = {
            "type": "image",
            "mimetype": "image/png",
            "data": base64.b64encode(raw).decode(),
            "mediaUrl": "http://example.com/img.png",
        }
        result = extract_media(msg)
        assert result is not None
        assert result.data == raw
        assert result.url is None

    def test_image_from_media_dict(self):
        # WAHA REST API with downloadMedia=true delivers media as a nested dict
        # with a url key (not the flat mediaUrl field).
        msg = {
            "type": "image",
            "mimetype": "image/jpeg",
            "media": {
                "url": "http://localhost:3000/api/files/default/img.jpg",
                "mimetype": "image/jpeg",
                "filename": None,
            },
        }
        result = extract_media(msg)
        assert result is not None
        assert result.type is MediaType.IMAGE
        assert result.url == "http://localhost:3000/api/files/default/img.jpg"
        assert result.data is None
        assert result.mime_type == "image/jpeg"

    def test_media_dict_mimetype_fallback(self):
        # If the top-level mimetype is missing, fall back to media.mimetype.
        msg = {
            "type": "image",
            "media": {
                "url": "http://localhost:3000/api/files/default/img.webp",
                "mimetype": "image/webp",
            },
        }
        result = extract_media(msg)
        assert result is not None
        assert result.mime_type == "image/webp"

    def test_media_dict_without_url_returns_none(self):
        msg = {"type": "image", "media": {"mimetype": "image/jpeg"}}
        assert extract_media(msg) is None
