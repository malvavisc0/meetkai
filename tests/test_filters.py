from kai.config.filters import should_process_chat_message


class TestShouldProcessChatMessage:
    def test_direct_message_allowed_without_lists(self):
        assert should_process_chat_message("123@c.us", "123@c.us", set(), set()) is True

    def test_direct_message_requires_chat_whitelist_match(self):
        assert should_process_chat_message("123@c.us", "123@c.us", {"999@c.us"}, set()) is False

    def test_group_message_allowed_by_chat_id_whitelist(self):
        assert should_process_chat_message("group@g.us", "123@c.us", {"group@g.us"}, set()) is True

    def test_group_message_allowed_by_author_whitelist(self):
        assert should_process_chat_message("group@g.us", "123@c.us", {"123@c.us"}, set()) is True

    def test_group_message_blocked_when_neither_chat_nor_author_whitelisted(self):
        assert should_process_chat_message("group@g.us", "123@c.us", {"other@g.us"}, set()) is False

    def test_blacklisted_group_blocks_whitelisted_author(self):
        assert (
            should_process_chat_message("group@g.us", "123@c.us", {"123@c.us"}, {"group@g.us"})
            is False
        )

    def test_blacklisted_author_blocks_whitelisted_group(self):
        assert (
            should_process_chat_message("group@g.us", "123@c.us", {"group@g.us"}, {"123@c.us"})
            is False
        )
