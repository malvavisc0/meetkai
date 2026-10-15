"""Tests for webhook replay protection (kai.cockpit.connections.webhooks.check_freshness)."""

import pytest

from kai.cockpit.connections.webhooks import (
    FRESHNESS_WINDOW_SECONDS,
    SEEN_NONCES,
    check_freshness,
    clear_seen_nonces,
    prune_seen,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_seen_nonces()
    yield
    clear_seen_nonces()


_NOW = 1_000_000.0


class TestFreshnessWindow:
    def test_current_timestamp_accepted(self):
        assert check_freshness(timestamp=_NOW, now=_NOW) is True

    def test_past_timestamp_rejected(self):
        assert check_freshness(timestamp=_NOW - 600, now=_NOW) is False

    def test_future_timestamp_rejected(self):
        assert check_freshness(timestamp=_NOW + 600, now=_NOW) is False

    def test_boundary_accepted(self):
        assert check_freshness(timestamp=_NOW - FRESHNESS_WINDOW_SECONDS, now=_NOW) is True
        assert check_freshness(timestamp=_NOW + FRESHNESS_WINDOW_SECONDS, now=_NOW) is True

    def test_just_outside_boundary_rejected(self):
        assert check_freshness(timestamp=_NOW - FRESHNESS_WINDOW_SECONDS - 1, now=_NOW) is False


class TestNonceDedup:
    def test_first_nonce_accepted(self):
        assert check_freshness(nonce="abc", now=_NOW) is True

    def test_replay_rejected(self):
        assert check_freshness(nonce="abc", now=_NOW) is True
        assert check_freshness(nonce="abc", now=_NOW) is False

    def test_different_nonce_accepted(self):
        assert check_freshness(nonce="abc", now=_NOW) is True
        assert check_freshness(nonce="xyz", now=_NOW) is True


class TestBothNone:
    def test_both_none_rejected(self):
        assert check_freshness(timestamp=None, nonce=None) is False


class TestTimestampAndNonce:
    def test_replay_rejected_even_if_timestamp_fresh(self):
        assert check_freshness(timestamp=_NOW, nonce="abc", now=_NOW) is True
        assert check_freshness(timestamp=_NOW, nonce="abc", now=_NOW + 10) is False

    def test_stale_timestamp_rejected_even_if_nonce_new(self):
        assert check_freshness(timestamp=_NOW - 600, nonce="new", now=_NOW) is False


class TestPruning:
    def test_prune_evicts_expired_entries(self):
        check_freshness(nonce="old", now=_NOW - 1000)
        assert SEEN_NONCES.seen("old")

        prune_seen(_NOW)
        assert not SEEN_NONCES.seen("old")

    def test_prune_keeps_fresh_entries(self):
        check_freshness(nonce="fresh", now=_NOW)
        prune_seen(_NOW)
        assert SEEN_NONCES.seen("fresh")

    def test_expired_nonce_can_be_reused_after_prune(self):
        check_freshness(nonce="recycled", now=_NOW - 1000)
        prune_seen(_NOW)
        assert not SEEN_NONCES.seen("recycled")
        assert check_freshness(nonce="recycled", now=_NOW) is True


class TestLruBound:
    def test_oldest_dropped_when_over_limit(self, monkeypatch):
        monkeypatch.setattr("kai.cockpit.connections.webhooks.SEEN_NONCES_MAX", 3)
        check_freshness(nonce="a", now=_NOW)
        check_freshness(nonce="b", now=_NOW + 1)
        check_freshness(nonce="c", now=_NOW + 2)
        assert SEEN_NONCES.count() == 3

        check_freshness(nonce="d", now=_NOW + 3)
        assert SEEN_NONCES.count() == 3
        assert not SEEN_NONCES.seen("a")
        assert SEEN_NONCES.seen("d")
