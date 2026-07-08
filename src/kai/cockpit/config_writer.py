"""Render a deployment's settings + feature flags into a BotConfig JSON file.

Written to ``data/configs/cockpit/<instance_id>.json`` (relative to the
cockpit CWD, i.e. ``/app/data/configs/cockpit/...`` in the container). The
filename MUST match what the spawned bot reads: ``BaseBot.resolve_config_path``
looks up ``<configs_dir>/<instance_id>.json``, where ``instance_id`` is
``{bot_type}-{user_email}`` (set by ``kai start --user <email>``). Writing
by deployment id would leave the file orphaned — the bot would never find it
and fall through to the packaged default.

The bot reads this at startup via ``KAI_CONFIGS_DIR=data/configs/cockpit`` —
both the cockpit and the spawned subprocess resolve it against the same data
root, so there is no separate configs/ volume.

``goal`` and ``voice`` are NOT written here — they're passed as CLI flags.
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

    ``instance_id`` is the per-bot namespace (``{bot_type}-{user_email}``)
    that the spawned bot process uses to locate its external config, so the
    filename here must equal what ``BaseBot.resolve_config_path`` probes.

    Returns the path written.
    """
    config = dict(deployment.settings)

    # Merge feature flags into media.*
    media = dict(config.get("media", {}))
    flags = deployment.feature_flags
    media["image_enabled"] = flags.get("image", False)
    media["stt_enabled"] = flags.get("stt", False)
    media["tts_enabled"] = flags.get("tts", False)
    media["video_enabled"] = flags.get("video", False)
    # Preserve instagram_enabled and max_size_mb (not cockpit flags in v1).
    # If the existing config file has them, read and merge.
    path = CONFIGS_DIR / f"{instance_id}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            existing_media = existing.get("media", {})
            if "instagram_enabled" in existing_media:
                media.setdefault("instagram_enabled", existing_media["instagram_enabled"])
            if "max_size_mb" in existing_media:
                media.setdefault("max_size_mb", existing_media["max_size_mb"])
        except (OSError, json.JSONDecodeError):
            pass
    # Defaults for fields not in cockpit flags and not preserved from file
    media.setdefault("instagram_enabled", True)
    media.setdefault("max_size_mb", 10)
    config["media"] = media

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
