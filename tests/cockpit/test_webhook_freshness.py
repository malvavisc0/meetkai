"""Tests for webhook replay protection (kai.cockpit.webhooks._check_freshness)."""

import pytest

from kai.cockpit.webhooks import (
    FRESHNESS_WINDOW_SECONDS,
    _check_freshness,
    _clear_seen_nonces,
    _prune_seen,
    _seen_nonces,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    _clear_seen_nonces()
    yield
    _clear_seen_nonces()


_NOW = 1_000_000.0


class TestFreshnessWindow:
    def test_current_timestamp_accepted(self):
        assert _check_freshness(timestamp=_NOW, now=_NOW) is True

    def test_past_timestamp_rejected(self):
        assert _check_freshness(timestamp=_NOW - 600, now=_NOW) is False

    def test_future_timestamp_rejected(self):
        assert _check_freshness(timestamp=_NOW + 600, now=_NOW) is False

    def test_boundary_accepted(self):
        assert _check_freshness(timestamp=_NOW - FRESHNESS_WINDOW_SECONDS, now=_NOW) is True
        assert _check_freshness(timestamp=_NOW + FRESHNESS_WINDOW_SECONDS, now=_NOW) is True

    def test_just_outside_boundary_rejected(self):
        assert _check_freshness(timestamp=_NOW - FRESHNESS_WINDOW_SECONDS - 1, now=_NOW) is False


class TestNonceDedup:
    def test_first_nonce_accepted(self):
        assert _check_freshness(nonce="abc", now=_NOW) is True

    def test_replay_rejected(self):
        assert _check_freshness(nonce="abc", now=_NOW) is True
        assert _check_freshness(nonce="abc", now=_NOW) is False

    def test_different_nonce_accepted(self):
        assert _check_freshness(nonce="abc", now=_NOW) is True
        assert _check_freshness(nonce="xyz", now=_NOW) is True


class TestBothNone:
    def test_both_none_rejected(self):
        assert _check_freshness(timestamp=None, nonce=None) is False


class TestTimestampAndNonce:
    def test_replay_rejected_even_if_timestamp_fresh(self):
        assert _check_freshness(timestamp=_NOW, nonce="abc", now=_NOW) is True
        assert _check_freshness(timestamp=_NOW, nonce="abc", now=_NOW + 10) is False

    def test_stale_timestamp_rejected_even_if_nonce_new(self):
        assert _check_freshness(timestamp=_NOW - 600, nonce="new", now=_NOW) is False


class TestPruning:
    def test_prune_evicts_expired_entries(self):
        _check_freshness(nonce="old", now=_NOW - 1000)
        assert "old" in _seen_nonces

        _prune_seen(_NOW)
        assert "old" not in _seen_nonces

    def test_prune_keeps_fresh_entries(self):
        _check_freshness(nonce="fresh", now=_NOW)
        _prune_seen(_NOW)
        assert "fresh" in _seen_nonces

    def test_expired_nonce_can_be_reused_after_prune(self):
        _check_freshness(nonce="recycled", now=_NOW - 1000)
        _prune_seen(_NOW)
        assert "recycled" not in _seen_nonces
        assert _check_freshness(nonce="recycled", now=_NOW) is True


class TestLruBound:
    def test_oldest_dropped_when_over_limit(self, monkeypatch):
        monkeypatch.setattr("kai.cockpit.webhooks._SEEN_NONCES_MAX", 3)
        _check_freshness(nonce="a", now=_NOW)
        _check_freshness(nonce="b", now=_NOW + 1)
        _check_freshness(nonce="c", now=_NOW + 2)
        assert len(_seen_nonces) == 3

        _check_freshness(nonce="d", now=_NOW + 3)
        assert len(_seen_nonces) == 3
        assert "a" not in _seen_nonces
        assert "d" in _seen_nonces
