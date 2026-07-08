"""Tests for kai.cockpit.config_writer.write_config()."""

import json

import pytest

from kai.cockpit import config_writer
from kai.cockpit.models import Deployment

# The instance_id the spawned bot process uses to locate its external config
# (``{bot_type}-{user_email}``). The cockpit MUST write under this stem so
# BaseBot.resolve_config_path() finds the file — writing by deployment id
# would orphan it.
INSTANCE_ID = "waha-bob@test.com"


@pytest.fixture(autouse=True)
def _isolated_configs_dir(tmp_path, monkeypatch):
    """Redirect CONFIGS_DIR to a tmp dir so tests never touch configs/cockpit/."""
    monkeypatch.setattr(config_writer, "CONFIGS_DIR", tmp_path / "configs" / "cockpit")
    return tmp_path


def _make_deployment(**overrides) -> Deployment:
    defaults = dict(
        id=1,
        user_id=1,
        bot_type="waha",
        goal="be helpful",
        language="English",
        voice="af_heart",
        feature_flags={"image": True, "stt": False, "tts": True, "video": True},
        settings={
            "trigger_keyword": "kai",
            "whitelist": [],
            "blacklist": [],
            "language": "English",
            "timezone": "UTC",
            "mentions_enabled": True,
            "participation": {"enabled": True, "rate": 0.15},
        },
        created_at="now",
        updated_at="now",
    )
    defaults.update(overrides)
    return Deployment(**defaults)


class TestWriteConfig:
    def test_writes_json_content(self):
        dep = _make_deployment()
        path = config_writer.write_config(dep, INSTANCE_ID)
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["trigger_keyword"] == "kai"
        assert data["language"] == "English"
        assert data["participation"] == {"enabled": True, "rate": 0.15}

    def test_feature_flags_merged_into_media(self):
        dep = _make_deployment()
        path = config_writer.write_config(dep, INSTANCE_ID)
        data = json.loads(path.read_text())
        assert data["media"]["image_enabled"] is True
        assert data["media"]["stt_enabled"] is False
        assert data["media"]["tts_enabled"] is True
        assert data["media"]["video_enabled"] is True

    def test_goal_and_voice_not_in_config(self):
        dep = _make_deployment()
        path = config_writer.write_config(dep, INSTANCE_ID)
        data = json.loads(path.read_text())
        assert "goal" not in data
        assert "voice" not in data

    def test_preserves_instagram_and_max_size_on_rewrite(self):
        dep = _make_deployment()
        path = config_writer.write_config(dep, INSTANCE_ID)
        data = json.loads(path.read_text())
        # Simulate an operator having hand-edited these non-cockpit fields.
        data["media"]["instagram_enabled"] = False
        data["media"]["max_size_mb"] = 25
        path.write_text(json.dumps(data))

        config_writer.write_config(dep, INSTANCE_ID)
        reloaded = json.loads(path.read_text())
        assert reloaded["media"]["instagram_enabled"] is False
        assert reloaded["media"]["max_size_mb"] == 25

    def test_defaults_when_no_existing_file(self):
        dep = _make_deployment()
        path = config_writer.write_config(dep, INSTANCE_ID)
        data = json.loads(path.read_text())
        assert data["media"]["instagram_enabled"] is True
        assert data["media"]["max_size_mb"] == 10

    def test_filename_matches_instance_id_not_dep_id(self):
        """The bot reads <instance_id>.json, so we must write that name."""
        dep = _make_deployment(id=42)
        path = config_writer.write_config(dep, INSTANCE_ID)
        assert path.name == f"{INSTANCE_ID}.json"
        assert path.name != "42.json"

    def test_preserve_uses_instance_id_filename(self):
        """instagram/max_size preservation must read the same instance_id file."""
        dep = _make_deployment()
        # Seed a file with preserved fields under the instance_id name.
        first = config_writer.write_config(dep, INSTANCE_ID)
        seeded = json.loads(first.read_text())
        seeded["media"]["instagram_enabled"] = False
        seeded["media"]["max_size_mb"] = 25
        first.write_text(json.dumps(seeded))

        config_writer.write_config(dep, INSTANCE_ID)
        reloaded = json.loads(first.read_text())
        assert reloaded["media"]["instagram_enabled"] is False
        assert reloaded["media"]["max_size_mb"] == 25
