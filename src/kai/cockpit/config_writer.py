"""Render a deployment's settings + feature flags into a BotConfig JSON file.

Written to ``data/configs/cockpit/<instance_id>.json``. The filename must
match what the spawned bot reads: ``BaseBot.resolve_config_path`` looks up
``<configs_dir>/<instance_id>.json``. The bot reads this at startup via
``KAI_CONFIGS_DIR=data/configs/cockpit``.
"""

import json
from pathlib import Path

from kai.cockpit.models import Deployment

CONFIGS_DIR = Path("data/configs/cockpit")


def write_config(deployment: Deployment, instance_id: str) -> Path:
    """Render deployment settings + feature flags into a BotConfig JSON file.

    ``instance_id`` is the per-bot namespace that the spawned bot process uses
    to locate its external config. Returns the path written.
    """
    config = dict(deployment.settings)
    flags = deployment.feature_flags

    if deployment.bot_type == "email":
        # Email: ``image`` feature flag maps to BotConfig.vision.
        config["vision"] = flags.get("image", False)

    path = CONFIGS_DIR / f"{instance_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
