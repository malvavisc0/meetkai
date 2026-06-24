from kai.bots.waha.mentions import resolve_inbound_mentions, resolve_mentions


class TestResolveMentionsGroup:
    def test_single_mention(self):
        roster = {"123456789012345@lid": "Juan Palotes"}
        result = resolve_mentions(
            "@[Juan Palotes] good point!", roster, bot_ids=set(), is_group=True
        )
        assert result.text == "@123456789012345 good point!"
        assert result.mentions == ["123456789012345@lid"]

    def test_multiple_mentions(self):
        roster = {
            "123456789012345@lid": "Juan Palotes",
            "72013750239365@lid": "Lucerna",
        }
        result = resolve_mentions(
            "@[Juan Palotes] and @[Lucerna] hi", roster, bot_ids=set(), is_group=True
        )
        assert result.text == "@123456789012345 and @72013750239365 hi"
        assert len(result.mentions) == 2

    def test_repeated_mention_deduped(self):
        roster = {"123456789012345@lid": "Juan Palotes"}
        result = resolve_mentions(
            "@[Juan Palotes] hey @[Juan Palotes]", roster, bot_ids=set(), is_group=True
        )
        assert result.text == "@123456789012345 hey @123456789012345"
        assert result.mentions == ["123456789012345@lid"]

    def test_unresolved_mention_becomes_plain_text(self):
        roster = {"123456789012345@lid": "Juan Palotes"}
        result = resolve_mentions("@[Ghost Person] hi", roster, bot_ids=set(), is_group=True)
        assert result.text == "Ghost Person hi"
        assert result.mentions == []

    def test_case_insensitive_match(self):
        roster = {"123456789012345@lid": "Juan Palotes"}
        result = resolve_mentions("@[juan palotes] hi", roster, bot_ids=set(), is_group=True)
        assert result.text == "@123456789012345 hi"
        assert result.mentions == ["123456789012345@lid"]

    def test_accent_insensitive_match(self):
        roster = {"123456789012345@lid": "Juan Pálotes"}
        result = resolve_mentions("@[Juan Palotes] hi", roster, bot_ids=set(), is_group=True)
        assert result.text == "@123456789012345 hi"
        assert result.mentions == ["123456789012345@lid"]

    def test_no_mentions_in_text(self):
        roster = {"123456789012345@lid": "Juan Palotes"}
        result = resolve_mentions("hello world", roster, bot_ids=set(), is_group=True)
        assert result.text == "hello world"
        assert result.mentions == []


class TestResolveMentionsBotSelfTag:
    def test_bot_mention_excluded(self):
        roster = {"123456789012345@lid": "Juan Palotes"}
        result = resolve_mentions(
            "@[Juan Palotes] hello", roster, bot_ids={"123456789012345@lid"}, is_group=True
        )
        assert result.text == "Juan Palotes hello"
        assert result.mentions == []

    def test_bot_cus_id_excluded(self):
        roster = {"123456789012345@lid": "Juan Palotes"}
        result = resolve_mentions(
            "@[Juan Palotes] hello", roster, bot_ids={"123456789012345@c.us"}, is_group=True
        )
        assert result.text == "Juan Palotes hello"
        assert result.mentions == []


class TestResolveMentionsDM:
    def test_strips_mentions_in_dm(self):
        roster = {"123456789012345@lid": "Juan Palotes"}
        result = resolve_mentions("@[Juan Palotes] hi", roster, bot_ids=set(), is_group=False)
        assert result.text == "Juan Palotes hi"
        assert result.mentions == []

    def test_multiple_mentions_stripped_in_dm(self):
        roster = {
            "123456789012345@lid": "Juan Palotes",
            "72013750239365@lid": "Lucerna",
        }
        result = resolve_mentions(
            "@[Juan Palotes] and @[Lucerna]", roster, bot_ids=set(), is_group=False
        )
        assert result.text == "Juan Palotes and Lucerna"
        assert result.mentions == []


class TestResolveMentionsCollision:
    def test_duplicate_names_left_as_plain_text(self):
        roster = {
            "111@lid": "Alex",
            "222@lid": "Alex",
        }
        result = resolve_mentions("@[Alex] hi", roster, bot_ids=set(), is_group=True)
        assert result.text == "Alex hi"
        assert result.mentions == []

    def test_unique_names_still_resolve(self):
        roster = {
            "111@lid": "Alex",
            "222@lid": "Jordan",
        }
        result = resolve_mentions("@[Alex] and @[Jordan] hi", roster, bot_ids=set(), is_group=True)
        assert "@111" in result.text
        assert "@222" in result.text
        assert len(result.mentions) == 2

    def test_first_name_collision_with_full_name_unique(self):
        roster = {
            "111@lid": "Alex Smith",
            "222@lid": "Alex Jones",
            "333@lid": "Jordan Lee",
        }
        result = resolve_mentions("@[Alex Smith] hi", roster, bot_ids=set(), is_group=True)
        assert result.text == "@111 hi"
        assert result.mentions == ["111@lid"]

    def test_bare_mention_resolves_unique_first_name(self):
        roster = {
            "111@lid": "Jordan Lee",
        }
        result = resolve_mentions("@Jordan hi", roster, bot_ids=set(), is_group=True)
        assert result.text == "@111 hi"
        assert result.mentions == ["111@lid"]


class TestResolveMentionsUnicode:
    def test_cyrillic_name_resolves(self):
        roster = {"999@lid": "Андрей"}
        result = resolve_mentions("@[Андрей] привет", roster, bot_ids=set(), is_group=True)
        assert result.text == "@999 привет"
        assert result.mentions == ["999@lid"]

    def test_arabic_name_resolves(self):
        roster = {"888@lid": "محمد"}
        result = resolve_mentions("@[محمد] مرحبا", roster, bot_ids=set(), is_group=True)
        assert result.text == "@888 مرحبا"
        assert result.mentions == ["888@lid"]

    def test_cjk_name_resolves(self):
        roster = {"777@lid": "田中太郎"}
        result = resolve_mentions("@[田中太郎] こんにちは", roster, bot_ids=set(), is_group=True)
        assert result.text == "@777 こんにちは"
        assert result.mentions == ["777@lid"]

    def test_distinct_non_latin_names_do_not_collide(self):
        roster = {
            "111@lid": "Андрей",
            "222@lid": "محمد",
            "333@lid": "田中太郎",
        }
        result = resolve_mentions(
            "@[Андрей] and @[محمد] and @[田中太郎]",
            roster,
            bot_ids=set(),
            is_group=True,
        )
        assert "@111" in result.text
        assert "@222" in result.text
        assert "@333" in result.text
        assert len(result.mentions) == 3


class TestResolveInboundMentions:
    def test_rewrites_digits_to_bracketed_name(self):
        roster = {"123456789012345@c.us": "Juan Palotes"}
        text = "@123456789012345 puso la semilla"
        assert resolve_inbound_mentions(text, roster, is_group=True) == (
            "@[Juan Palotes] puso la semilla"
        )

    def test_multiple_mentions_in_one_message(self):
        roster = {
            "123456789012345@c.us": "Juan Palotes",
            "72013750239365@lid": "Lucerna",
        }
        text = "@123456789012345 y @72013750239365 hablaron"
        assert resolve_inbound_mentions(text, roster, is_group=True) == (
            "@[Juan Palotes] y @[Lucerna] hablaron"
        )

    def test_unmatched_digits_left_untouched(self):
        roster = {"123456789012345@c.us": "Juan Palotes"}
        # A phone number not on the roster stays as-is.
        text = "llama al 5550000 o @999999999999999"
        result = resolve_inbound_mentions(text, roster, is_group=True)
        assert "@999999999999999" in result

    def test_short_digit_runs_not_matched(self):
        roster = {"111@c.us": "Shorty"}
        # <5 digits (e.g. "@2h") must not be rewritten.
        text = "vuelvo en @2h"
        assert resolve_inbound_mentions(text, roster, is_group=True) == "vuelvo en @2h"

    def test_dm_not_rewritten(self):
        roster = {"123456789012345@c.us": "Juan Palotes"}
        text = "@123456789012345 hi"
        # DMs don't carry mention payloads; leave text alone.
        assert resolve_inbound_mentions(text, roster, is_group=False) == text

    def test_empty_roster_passthrough(self):
        assert resolve_inbound_mentions("@123456 hi", {}, is_group=True) == "@123456 hi"

    def test_empty_text(self):
        roster = {"123456789012345@c.us": "Juan Palotes"}
        assert resolve_inbound_mentions("", roster, is_group=True) == ""

    def test_lid_suffix_jid_resolves(self):
        roster = {"72013750239365@lid": "Lucerna"}
        text = "@72013750239365 rn estos momentos"
        assert resolve_inbound_mentions(text, roster, is_group=True) == (
            "@[Lucerna] rn estos momentos"
        )

    def test_round_trip_with_outbound_resolver(self):
        # Inbound rewrites @digits -> @[Name]; outbound resolves @[Name] back
        # to @digits for WhatsApp delivery. The two are inverse on the text.
        roster = {"123456789012345@c.us": "Juan Palotes"}
        inbound = "@123456789012345 puso la semilla"
        for_model = resolve_inbound_mentions(inbound, roster, is_group=True)
        assert for_model == "@[Juan Palotes] puso la semilla"
        out = resolve_mentions(for_model, roster, bot_ids=set(), is_group=True)
        assert out.text == "@123456789012345 puso la semilla"
        assert out.mentions == ["123456789012345@c.us"]
