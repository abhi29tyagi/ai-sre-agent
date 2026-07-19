"""Event query routes."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.routes.webhooks import require_webhook_token
from app.services.intake import fetch_recent_events
from app.services.rca_rerun import RcaRerunFailure, execute_rca_rerun

logger = logging.getLogger(__name__)
router = APIRouter()


class RcaRerunBody(BaseModel):
    """Optional body for POST /events/{id}/rca/rerun."""

    model: Optional[str] = Field(default=None, description="Ollama model tag for this run only (must exist on server)")
    user_prompt: Optional[str] = Field(
        default=None,
        description="Extra instructions for this RCA run (appended to the prompt; length-capped by env)",
    )
    notify_slack: Optional[bool] = Field(
        default=None,
        description="Post full digest to Slack — omit to use NOTIFY_SLACK_ON_RCA_RERUN env; explicit true/false overrides",
    )


@router.get("/events/recent")
async def events_recent(
    limit: int = Query(default=20, ge=1, le=200),
    x_webhook_token: Optional[str] = Header(default=None, alias="X-Webhook-Token"),
) -> Dict[str, Any]:
    require_webhook_token(x_webhook_token)
    try:
        events = await fetch_recent_events(limit=limit)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception:  # pragma: no cover
        logger.exception("failed to fetch recent events")
        raise HTTPException(status_code=500, detail="failed to fetch events")

    return {"count": len(events), "items": events}


@router.post("/events/{event_id}/rca/rerun")
async def rca_rerun(
    event_id: int,
    body: Optional[RcaRerunBody] = Body(default=None),
    x_webhook_token: Optional[str] = Header(default=None, alias="X-Webhook-Token"),
) -> Dict[str, Any]:
    """
    Regenerate Ollama RCA from stored Grafana payload + Loki enrichment (same log window).

    Auth: same ``WEBHOOK_SECRET`` / ``X-Webhook-Token`` as ``POST /hooks/grafana`` when configured.

    Optional JSON body fields: ``model``, ``user_prompt`` (operator instructions for this run),
    ``notify_slack``.

    Call from curl, automation, Slack Workflow (HTTP step), or a **Slack thread reply** via Events API.
    """
    require_webhook_token(x_webhook_token)

    req = body or RcaRerunBody()
    override = req.model.strip() if isinstance(req.model, str) and req.model.strip() else None
    try:
        return await execute_rca_rerun(
            event_id,
            model=override,
            user_prompt=req.user_prompt,
            notify_slack=req.notify_slack,
        )
    except RcaRerunFailure as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
