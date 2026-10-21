"""Bot HTTP calls and on-disk history for deployments."""

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from kai.cockpit.deployment_helpers import normalize_msg, valid_msg
from kai.cockpit.models import Deployment
from kai.runs import RunRecord
from kai.utils.common import compute_hmac

if TYPE_CHECKING:
    from kai.cockpit.deployments import DeploymentsService

logger = logging.getLogger(__name__)


def sign_body(record: RunRecord, body: bytes) -> str:
    """Compute the HMAC signature for a request body sent to the bot."""
    return compute_hmac(record.hmac_key, body, record.hmac_algorithm)


def call_bot(
    record: RunRecord,
    method: str,
    path: str,
    body: bytes = b"",
    *,
    timeout: float = 30.0,
    sign: Callable[[RunRecord, bytes], str] = sign_body,
) -> dict:
    """Make an HMAC-signed HTTP call to the running bot.

    Returns the JSON response dict on success, or an error dict on failure.
    """
    signature = sign(record, body)
    headers: dict[str, str] = {"X-Webhook-Hmac": signature}
    if body:
        headers["Content-Type"] = "application/json"
    try:
        resp = httpx.request(
            method,
            f"{record.endpoint}{path}",
            content=body if body else None,
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}


def forward_event(
    svc: "DeploymentsService", deployment: Deployment, path: str, body: bytes
) -> bool:
    """Forward a normalized inbound event to a running deployment's bot.

    Returns True if the bot accepted the event, False if the bot isn't
    reachable or rejected it. ``call_bot`` returns ``{"ok": False, ...}``
    on every failure shape, so ``False`` is the one failure signal.
    """
    record = svc.resolve_run(deployment)
    if record is None:
        return False
    result = svc.call_bot(record, "POST", path, body)
    return result.get("ok", True) is not False


def send_message(
    svc: "DeploymentsService", deployment: Deployment, message: str, persist: bool = False
) -> dict:
    """Forward an operator message to the running bot's /tell route."""
    record = svc.resolve_run(deployment)
    if record is None:
        return {"ok": False, "reply": "bot is not running"}

    body = json.dumps({"message": message, "persist": persist}).encode()
    return svc.call_bot(record, "POST", "/tell", body, timeout=120.0)


def clear_history(svc: "DeploymentsService", deployment: Deployment) -> dict:
    """Clear the bot's conversation history."""
    record = svc.resolve_run(deployment)
    if record is None:
        return {"ok": False, "error": "bot is not running"}

    return svc.call_bot(record, "POST", "/clear")


def history(svc: "DeploymentsService", deployment: Deployment) -> dict[str, list[dict]]:
    """Load per-bot history and return ``{chat_id: [messages]}``.

    Merges per-conversation files in ``<instance>.history/`` with the legacy
    ``<instance>.json`` file and returns ``{}`` when neither exists. Each
    message is ``{"role": str, "content": str, "ts": str | None}``; ``ts`` is
    the ISO-8601 UTC timestamp recorded when the message was stored, and
    older history files (pre-timestamp) have ``None``. Files are written
    atomically by the bot process (``.tmp`` replace), so reading is safe.

    On-disk keys are namespaced as ``{instance_id}:{conversation_id}``
    (see :meth:`KaiAgent.history_key``); the returned dict strips the
    instance prefix so the cockpit surfaces the raw conversation id
    (e.g. ``operator``, ``1809...@g.us``).
    """
    instance_id = svc.deployment_instance_id(deployment)
    raw = svc.load_history_raw(deployment, instance_id)
    if not isinstance(raw, dict):
        return {}
    history_data = raw.get("history", raw)
    if not isinstance(history_data, dict):
        return {}
    return svc.normalize_history(history_data, instance_id)


def read_history_dir(history_dir: Path) -> dict[str, list] | None:
    """Merge per-conversation history files; None when the directory is absent."""
    if not history_dir.is_dir():
        return None
    merged: dict[str, list] = {}
    for path in sorted(history_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("failed to read history file %s", path)
            continue
        if not isinstance(raw, dict):
            continue
        key = raw.get("key")
        messages = raw.get("messages")
        if isinstance(key, str) and isinstance(messages, list):
            merged[key] = messages
    return merged


def read_history_file(path: Path) -> object:
    """Read a legacy single-file history JSON, or return {} when absent."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("failed to read history file %s", path)
        return {}


def history_mapping(raw: object) -> dict[str, list]:
    """Extract a validated ``{conversation: messages}`` mapping from raw JSON."""
    if not isinstance(raw, dict):
        return {}
    data = raw.get("history", raw)
    if not isinstance(data, dict):
        return {}
    return {
        key: messages
        for key, messages in data.items()
        if isinstance(key, str) and isinstance(messages, list)
    }


def load_history_raw(
    svc: "DeploymentsService", deployment: Deployment, instance_id: str | None = None
) -> object:
    """Read the raw history for a deployment instance.

    Merges the per-conversation ``<instance>.history/`` directory with the
    legacy ``<instance>.json`` file. Directory entries win per conversation,
    so a crash during migration or a rollback to the old layout never hides
    either source.
    """
    from kai.config.settings import get_settings

    folder = get_settings().agent_history_folder
    if folder is None:
        return {}
    iid = instance_id or svc.deployment_instance_id(deployment)
    merged = history_mapping(read_history_file(Path(folder) / f"{iid}.json"))
    dir_data = read_history_dir(Path(folder) / f"{iid}.history")
    if dir_data:
        merged.update(dir_data)
    return {"history": merged} if merged else {}


def normalize_history(history_data: dict, instance_id: str) -> dict[str, list[dict]]:
    """Strip the instance prefix and normalize stored messages."""
    prefix = f"{instance_id}:"
    result: dict[str, list[dict]] = {}
    for key, messages in history_data.items():
        if not isinstance(key, str) or not isinstance(messages, list):
            continue
        chat_id = key[len(prefix) :] if key.startswith(prefix) else key
        result[chat_id] = [normalize_msg(m) for m in messages if valid_msg(m)]
    return result


def interaction_summary(svc: "DeploymentsService", deployment: Deployment) -> tuple[int, int]:
    """Return ``(conversation count, message count)`` from on-disk history."""
    history_data = svc.history(deployment)
    conversation_count = len(history_data)
    message_count = sum(len(msgs) for msgs in history_data.values())
    return conversation_count, message_count
