"""Tests for kai.cockpit.naming — the shared kai-v<version>-<slug> scheme."""

import kai.cockpit.naming as naming_mod
from kai.cockpit.naming import kai_slug_for, kai_version_slug


class TestKaiVersionSlug:
    def test_matches_installed_package_version_digits(self):
        # kai's pyproject.toml pins version = "0.0.1" -> "001" (dots stripped).
        assert kai_version_slug() == "001"

    def test_falls_back_when_package_not_found(self, monkeypatch):
        def _raise(_name):
            raise naming_mod.PackageNotFoundError

        monkeypatch.setattr(naming_mod, "version", _raise)
        assert kai_version_slug() == naming_mod._FALLBACK_VERSION

    def test_tracks_version_bumps_dynamically(self, monkeypatch):
        # Simulate a future release — the slug must follow it without any
        # hardcoded constant needing to change.
        monkeypatch.setattr(naming_mod, "version", lambda _name: "2.10.3")
        assert kai_version_slug() == "2103"


class TestKaiSlugFor:
    def test_uses_dynamic_version_by_default(self):
        assert kai_slug_for("bob@test.com") == "kai-v001-bob_at_test_com"

    def test_reflects_a_simulated_version_bump(self, monkeypatch):
        monkeypatch.setattr(naming_mod, "version", lambda _name: "2.0.0")
        assert kai_slug_for("bob@test.com") == "kai-v200-bob_at_test_com"

    def test_explicit_version_overrides_dynamic_default(self):
        assert kai_slug_for("bob@test.com", version="999") == "kai-v999-bob_at_test_com"

    def test_sanitizes_illegal_characters(self):
        assert kai_slug_for("a+b@sub.example.co.uk") == "kai-v001-a-b_at_sub_example_co_uk"
