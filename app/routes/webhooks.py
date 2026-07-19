"""Webhook ingress routes."""

from __future__ import annotations

import hmac
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.services.intake import normalize_grafana_payload, persist_event, save_loki_enrichment
from app.services.loki import enrich_grafana_alert
from app.services.ollama_rca import generate_rca_ollama
from app.services.slack_notify import notify_grafana_alert as notify_slack
from app.services.webhook_notify_overrides import resolve_grafana_webhook_notify_flags

logger = logging.getLogger(__name__)


def require_webhook_token(x_webhook_token: Optional[str]) -> None:
    """Constant-time verify of X-Webhook-Token; no-op when WEBHOOK_SECRET is empty."""
    if not settings.webhook_secret:
        return
    if not x_webhook_token or not hmac.compare_digest(
        x_webhook_token.encode("utf-8"), settings.webhook_secret.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="invalid or missing webhook token")


def _public_enrichment_snapshot(enrichment: Dict[str, Any]) -> Dict[str, Any]:
    """Small JSON safe to return from webhook without dumping full log lines."""
    snap: Dict[str, Any] = {}
    if enrichment.get("skipped"):
        snap["skipped"] = True
        snap["reason"] = enrichment.get("reason")
    else:
        snap["skipped"] = False
        snap["environment"] = enrichment.get("environment")
        snap["routing_reason"] = enrichment.get("routing_reason")
        snap["loki_base_url"] = enrichment.get("loki_base_url")
        snap["query"] = enrichment.get("query")
        snap["error"] = enrichment.get("error")
        snap["has_samples"] = bool(enrichment.get("data"))
        if enrichment.get("window_anchor_note"):
            snap["window_anchor_note"] = enrichment.get("window_anchor_note")
        if enrichment.get("query_direction"):
            snap["query_direction"] = enrichment.get("query_direction")

    rca = enrichment.get("rca_ollama")
    if isinstance(rca, dict):
        if rca.get("text"):
            txt = str(rca.get("text", ""))
            snap["rca"] = {
                "model": rca.get("model"),
                "text_preview": txt[:600] + ("…" if len(txt) > 600 else ""),
            }
        elif rca.get("error"):
            snap["rca"] = {"error": rca.get("error")}
        elif rca.get("skipped_reason"):
            snap["rca"] = {"skipped": rca.get("skipped_reason")}
    return snap


router = APIRouter()


@router.post("/hooks/grafana")
async def hooks_grafana(
    request: Request,
    x_webhook_token: Optional[str] = Header(default=None, alias="X-Webhook-Token"),
) -> JSONResponse:
    require_webhook_token(x_webhook_token)

    try:
        body: Any = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JSON payload") from exc

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected JSON object payload")

    summary = normalize_grafana_payload(body)

    effective_slack, effective_ollama = resolve_grafana_webhook_notify_flags(
        body,
        request.query_params,
        settings_slack_notify=settings.slack_notify_enabled,
        settings_ollama_rca=settings.ollama_rca_enabled,
    )

    event_id = await persist_event(body, summary)

    enrichment: Optional[Dict[str, Any]] = None
    if event_id is not None:
        enrichment = await enrich_grafana_alert(body, summary)
        if not effective_ollama:
            rca_ollama: Dict[str, Any] = {"skipped_reason": "ollama_rca_webhook_override"}
        elif settings.ollama_rca_enabled:
            rca_ollama = await generate_rca_ollama(body, summary, enrichment)
        else:
            rca_ollama = {"skipped_reason": "ollama_rca_disabled"}
        enrichment = {**enrichment, "rca_ollama": rca_ollama}
        await save_loki_enrichment(event_id, enrichment)

    logger.info(
        "grafana webhook received status=%s alertname=%s alerts=%s group_key=%s fingerprint=%s event_id=%s "
        "effective_slack_notify=%s effective_ollama_rca=%s",
        summary.get("status"),
        summary.get("alertname"),
        summary.get("alert_count"),
        summary.get("group_key"),
        summary.get("fingerprint"),
        event_id,
        effective_slack,
        effective_ollama,
    )

    content: Dict[str, Any] = {
        "received": True,
        "event_id": event_id,
        "status": summary.get("status"),
        "alert_count": summary.get("alert_count"),
        "environment": summary.get("environment"),
        "webhook_notify": {"slack": effective_slack, "ollama_rca": effective_ollama},
    }
    if enrichment is not None:
        content["loki_enrichment"] = _public_enrichment_snapshot(enrichment)

    try:
        slack_meta = await notify_slack(
            summary,
            enrichment,
            event_id,
            slack_notify_enabled=effective_slack,
        )
    except Exception:  # pragma: no cover
        logger.exception("slack notification failed")
        slack_meta = {"sent": False, "error": "exception"}
    content["slack"] = slack_meta

    return JSONResponse(status_code=200, content=content)
