"""Conversation history page: ``GET /deployments/{dep_id}/history``."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from kai.cockpit.app import templates
from kai.cockpit.auth import require_user
from kai.cockpit.db import get_db
from kai.cockpit.deployments import DeploymentsService
from kai.cockpit.models import User
from kai.cockpit.routes.deployments._shared import fmt_ts, get_deployment

router = APIRouter()


def _conv_sort_key(item: tuple[str, list[dict]]) -> tuple[int, str]:
    """Sort conversations by their latest message timestamp (newest first),
    falling back to chat_id for legacy timestamp-less buckets so the page
    is deterministic.
    """
    chat_id, msgs = item
    last_ts = ""
    for m in reversed(msgs):
        ts = m.get("ts")
        if ts:
            last_ts = ts
            break
    # Timestamps sort lexicographically as ISO-8601 UTC; empty (legacy)
    # buckets sort last.
    return (1 if last_ts else 0, last_ts or chat_id)


@router.get("/deployments/{dep_id}/history")
async def deployment_history(
    request: Request,
    dep_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = DeploymentsService(db)
    result = get_deployment(svc, dep_id, user)
    if isinstance(result, RedirectResponse):
        return result
    svc, dep = result

    history_raw = svc.history(dep)
    total = sum(len(msgs) for msgs in history_raw.values())

    # Within each bucket messages are reversed so the latest message
    # appears at the top — no scrolling to the bottom.
    history: dict[str, list[dict]] = {}
    for chat_id, msgs in sorted(history_raw.items(), key=_conv_sort_key, reverse=True):
        history[chat_id] = [{**m, "ts": fmt_ts(m.get("ts"))} for m in reversed(msgs)]

    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "user": user,
            "dep": dep,
            "dep_user": user,
            "history": history,
            "total": total,
        },
    )
