"""Render a deployment's settings + feature flags into a BotConfig JSON file.

Written to ``data/configs/cockpit/<instance_id>.json``. The filename must
match what the spawned bot reads: ``BaseBot.resolve_config_path`` looks up
``<configs_dir>/<instance_id>.json``. The bot reads this at startup via
``KAI_CONFIGS_DIR=data/configs/cockpit``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kai.cockpit.models import Deployment

CONFIGS_DIR = Path("data/configs/cockpit")


def write_config(deployment: Deployment, instance_id: str) -> Path:
    """Render deployment settings + feature flags into a BotConfig JSON file.

    ``instance_id`` is the per-bot namespace that the spawned bot process uses
    to locate its external config. Returns the path written.
    """
    config = dict(deployment.settings)
    flags = deployment.feature_flags

    if "media" in config or deployment.bot_type == "waha":
        media = dict(config.get("media", {}))
        media["image_enabled"] = flags.get("image", False)
        media["stt_enabled"] = flags.get("stt", False)
        media["tts_enabled"] = flags.get("tts", False)
        media["video_enabled"] = flags.get("video", False)
        # Preserve instagram_enabled and max_size_mb from existing config file.
        existing_path = CONFIGS_DIR / f"{instance_id}.json"
        if existing_path.exists():
            try:
                existing = json.loads(existing_path.read_text(encoding="utf-8"))
                existing_media = existing.get("media", {})
                if "instagram_enabled" in existing_media:
                    media.setdefault("instagram_enabled", existing_media["instagram_enabled"])
                if "max_size_mb" in existing_media:
                    media.setdefault("max_size_mb", existing_media["max_size_mb"])
            except (OSError, json.JSONDecodeError):
                pass
        media.setdefault("instagram_enabled", True)
        media.setdefault("max_size_mb", 10)
        config["media"] = media
    elif deployment.bot_type == "email":
        # Email: ``image`` feature flag maps to BotConfig.vision.
        config["vision"] = flags.get("image", False)

    path = CONFIGS_DIR / f"{instance_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
