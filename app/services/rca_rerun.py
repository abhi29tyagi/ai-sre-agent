"""Shared RCA rerun logic for HTTP and Slack-triggered flows."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.config import settings
from app.services.intake import (
    fetch_event_by_id,
    normalize_grafana_payload,
    save_loki_enrichment,
)
from app.services.ollama_rca import generate_rca_ollama
from app.services.slack_notify import notify_grafana_alert

logger = logging.getLogger(__name__)


def _normalize_rca_user_prompt(raw: Optional[str]) -> Optional[str]:
    """Strip and cap operator instructions for RCA rerun (Slack / API)."""
    if raw is None or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    max_c = settings.ollama_rca_user_prompt_max_chars
    if len(s) > max_c:
        return s[:max_c].rstrip() + "\n... [truncated]"
    return s


class RcaRerunFailure(Exception):
    """Business failure with HTTP-style status and detail."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


async def execute_rca_rerun(
    event_id: int,
    *,
    model: Optional[str] = None,
    user_prompt: Optional[str] = None,
    notify_slack: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Regenerate Ollama RCA from stored Grafana payload + Loki enrichment.

    ``notify_slack``: if omitted (``None``), uses ``NOTIFY_SLACK_ON_RCA_RERUN`` from env;
    if ``True``/``False``, that wins over env.

    ``user_prompt``: optional operator instructions appended to the RCA prompt (length-capped).

    Raises ``RcaRerunFailure`` for missing row, bad payload, or missing enrichment.
    """
    event = await fetch_event_by_id(event_id)
    if event is None:
        raise RcaRerunFailure(404, "event not found")

    enrichment = event.get("loki_enrichment")
    if not isinstance(enrichment, dict):
        raise RcaRerunFailure(
            400,
            "event has no stored Loki enrichment; rerun needs a prior webhook with enrichment",
        )

    payload_json = event.get("payload_json")
    if not isinstance(payload_json, dict):
        raise RcaRerunFailure(500, "invalid stored payload")

    summary = normalize_grafana_payload(payload_json)

    override = model.strip() if isinstance(model, str) and model.strip() else None
    prompt_use = _normalize_rca_user_prompt(user_prompt)
    ollama_out = await generate_rca_ollama(
        payload_json,
        summary,
        enrichment,
        model_override=override,
        user_prompt=prompt_use,
    )

    merged: Dict[str, Any] = {
        **enrichment,
        "rca_ollama": ollama_out,
        "rca_rerun_at": datetime.now(timezone.utc).isoformat(),
    }
    if override:
        merged["rca_rerun_model_requested"] = override
    if prompt_use:
        merged["rca_rerun_user_prompt"] = prompt_use

    await save_loki_enrichment(event_id, merged)

    post_digest = settings.notify_slack_on_rca_rerun if notify_slack is None else notify_slack
    slack_meta: Optional[Dict[str, Any]] = None
    if post_digest:
        slack_meta = await notify_grafana_alert(summary, merged, event_id)

    out: Dict[str, Any] = {
        "ok": True,
        "event_id": event_id,
        "rca_ollama": ollama_out,
        "slack": slack_meta,
    }
    if prompt_use:
        out["rca_rerun_user_prompt"] = prompt_use
    return out
