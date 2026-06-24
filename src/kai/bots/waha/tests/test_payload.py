from kai.bots.waha.payload import parse_message


def _group_payload(
    notify_name: str = "Juan Palotes",
    author: str = "123456789012345@lid",
    body: str = "hello",
    mentioned_jid_list: list[str] | None = None,
) -> dict:
    return {
        "payload": {
            "from": "12345678901-1234567890@g.us",
            "participant": author,
            "body": body,
            "type": "chat",
            "_data": {
                "notifyName": notify_name,
                "author": {
                    "server": "lid",
                    "user": author.split("@")[0],
                    "_serialized": author,
                },
                "mentionedJidList": mentioned_jid_list or [],
                "groupMentions": [],
            },
        }
    }


def _dm_payload(
    notify_name: str = "Lucerna",
    from_id: str = "72013750239365@lid",
    body: str = "hi",
) -> dict:
    return {
        "payload": {
            "from": from_id,
            "body": body,
            "type": "chat",
            "_data": {
                "notifyName": notify_name,
                "mentionedJidList": [],
                "groupMentions": [],
            },
        }
    }


class TestParseMessageGroup:
    def test_extracts_sender_name(self):
        meta = parse_message(_group_payload())
        assert meta.sender_name == "Juan Palotes"

    def test_extracts_sender_id_from_author(self):
        meta = parse_message(_group_payload())
        assert meta.sender_id == "123456789012345@lid"

    def test_is_group_true(self):
        meta = parse_message(_group_payload())
        assert meta.is_group is True

    def test_chat_id_is_group(self):
        meta = parse_message(_group_payload())
        assert meta.chat_id == "12345678901-1234567890@g.us"


class TestParseMessageDM:
    def test_extracts_sender_name(self):
        meta = parse_message(_dm_payload())
        assert meta.sender_name == "Lucerna"

    def test_extracts_sender_id_from_from_field(self):
        meta = parse_message(_dm_payload())
        assert meta.sender_id == "72013750239365@lid"

    def test_is_group_false(self):
        meta = parse_message(_dm_payload())
        assert meta.is_group is False


class TestParseMessageMentionsBot:
    def test_mentions_bot_when_bot_in_mentioned_list(self):
        payload = _group_payload(mentioned_jid_list=["123456789012345@lid"])
        meta = parse_message(payload, bot_ids={"123456789012345@lid"})
        assert meta.mentions_bot is True

    def test_no_mention_when_bot_not_in_list(self):
        payload = _group_payload(mentioned_jid_list=["999999@lid"])
        meta = parse_message(payload, bot_ids={"123456789012345@lid"})
        assert meta.mentions_bot is False

    def test_mentions_bot_with_cus_id(self):
        payload = _group_payload(mentioned_jid_list=["123456789012345@lid"])
        meta = parse_message(payload, bot_ids={"123456789012345@c.us"})
        assert meta.mentions_bot is True

    def test_no_bot_ids_means_no_mention(self):
        payload = _group_payload(mentioned_jid_list=["123456789012345@lid"])
        meta = parse_message(payload, bot_ids=None)
        assert meta.mentions_bot is False

    def test_mentions_bot_from_dict_jid(self):
        dict_jid = {
            "server": "lid",
            "user": "123456789012345",
            "_serialized": "123456789012345@lid",
        }
        payload = _group_payload(mentioned_jid_list=[dict_jid])
        meta = parse_message(payload, bot_ids={"123456789012345@lid"})
        assert meta.mentions_bot is True

    def test_dict_jid_without_serialized_ignored(self):
        dict_jid = {"server": "lid", "user": "123456789012345"}
        payload = _group_payload(mentioned_jid_list=[dict_jid])
        meta = parse_message(payload, bot_ids={"123456789012345@lid"})
        assert meta.mentions_bot is False


class TestParseMessageFallbacks:
    def test_fallback_name_when_no_notify_name(self):
        payload = _group_payload(notify_name="")
        payload["payload"]["_data"].pop("notifyName")
        meta = parse_message(payload)
        assert meta.sender_name == "123456789012345"

    def test_sanitize_brackets_in_name(self):
        meta = parse_message(_group_payload(notify_name="Bad]Name["))
        assert "]" not in meta.sender_name
        assert "[" not in meta.sender_name

    def test_sanitize_newlines_in_name(self):
        meta = parse_message(_group_payload(notify_name="Line1\nLine2"))
        assert "\n" not in meta.sender_name

    def test_wrapped_payload_without_payload_key(self):
        payload = {
            "from": "12345678901-1234567890@g.us",
            "participant": "123456789012345@lid",
            "body": "hello",
            "type": "chat",
            "_data": {
                "notifyName": "Juan Palotes",
                "author": {
                    "server": "lid",
                    "user": "123456789012345",
                    "_serialized": "123456789012345@lid",
                },
                "mentionedJidList": [],
                "groupMentions": [],
            },
        }
        meta = parse_message(payload)
        assert meta.sender_name == "Juan Palotes"
