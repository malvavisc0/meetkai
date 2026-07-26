"""Tests for kai.cockpit.config_writer.write_config()."""

import json

import pytest

from kai.cockpit import config_writer
from kai.cockpit.models import Deployment

# The instance_id the spawned bot process uses to locate its external config
# (``{bot_type}-{user_email}``). The cockpit MUST write under this stem so
# BaseBot.resolve_config_path() finds the file — writing by deployment id
# would orphan it.
INSTANCE_ID = "email-bob@test.com"


@pytest.fixture(autouse=True)
def _isolated_configs_dir(tmp_path, monkeypatch):
    """Redirect CONFIGS_DIR to a tmp dir so tests never touch configs/cockpit/."""
    monkeypatch.setattr(config_writer, "CONFIGS_DIR", tmp_path / "configs" / "cockpit")
    return tmp_path


def _make_deployment(**overrides) -> Deployment:
    defaults = dict(
        id=1,
        user_id=1,
        bot_type="email",
        goal="be helpful",
        language="English",
        feature_flags={"image": True},
        settings={"language": "English", "display_name": "kAI"},
        created_at="now",
        updated_at="now",
    )
    defaults.update(overrides)
    return Deployment(**defaults)


class TestEmailVisionFlag:
    """Email deployments map the ``image`` feature flag to BotConfig.vision
    via config.json."""

    def test_vision_true_when_image_flag_on(self):
        dep = _make_deployment(
            bot_type="email",
            feature_flags={"image": True},
            settings={"blacklist": [], "display_name": "kAI"},
        )
        path = config_writer.write_config(dep, INSTANCE_ID)
        data = json.loads(path.read_text())
        assert data["vision"] is True

    def test_vision_false_when_image_flag_off(self):
        dep = _make_deployment(
            bot_type="email",
            feature_flags={"image": False},
            settings={"blacklist": [], "display_name": "kAI"},
        )
        path = config_writer.write_config(dep, INSTANCE_ID)
        data = json.loads(path.read_text())
        assert data["vision"] is False

    def test_email_does_not_get_media_block(self):
        dep = _make_deployment(
            bot_type="email",
            feature_flags={"image": True},
            settings={"blacklist": []},
        )
        path = config_writer.write_config(dep, INSTANCE_ID)
        data = json.loads(path.read_text())
        assert "media" not in data

    def test_filename_matches_instance_id_not_dep_id(self):
        """The bot reads <instance_id>.json, so we must write that name."""
        dep = _make_deployment(id=42)
        path = config_writer.write_config(dep, INSTANCE_ID)
        assert path.name == f"{INSTANCE_ID}.json"
        assert path.name != "42.json"
