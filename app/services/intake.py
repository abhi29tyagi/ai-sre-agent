"""Normalization and persistence logic for incoming alert events (async)."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from app.db import db_pool, get_connection
from app.services.loki import extract_environment_from_alert

logger = logging.getLogger(__name__)


def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_grafana_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    alerts = payload.get("alerts") if isinstance(payload.get("alerts"), list) else []
    first_alert = alerts[0] if alerts and isinstance(alerts[0], dict) else {}
    labels = first_alert.get("labels") if isinstance(first_alert.get("labels"), dict) else {}
    annotations = first_alert.get("annotations") if isinstance(first_alert.get("annotations"), dict) else {}
    environment = extract_environment_from_alert(first_alert) if isinstance(first_alert, dict) else None
    annotation_summary = (
        annotations.get("summary")
        or annotations.get("description")
        or annotations.get("message")
        or ""
    )
    if isinstance(annotation_summary, str):
        annotation_summary = annotation_summary.strip()
    else:
        annotation_summary = ""

    return {
        "status": str(payload.get("status") or first_alert.get("status") or "unknown"),
        "group_key": payload.get("groupKey"),
        "fingerprint": first_alert.get("fingerprint"),
        "alertname": labels.get("alertname") or payload.get("title"),
        "org_id": _to_int(payload.get("orgId")),
        "alert_count": len(alerts),
        "environment": environment,
        "annotation_summary": annotation_summary,
    }


async def persist_event(payload: Dict[str, Any], summary: Dict[str, Any]) -> Optional[int]:
    if db_pool() is None:
        return None
    try:
        async with get_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO incoming_alert_events (
                        source,
                        status,
                        alertname,
                        group_key,
                        fingerprint,
                        org_id,
                        alert_count,
                        payload_json
                    ) VALUES (
                        'grafana',
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s::jsonb
                    )
                    RETURNING id;
                    """,
                    (
                        summary.get("status"),
                        summary.get("alertname"),
                        summary.get("group_key"),
                        summary.get("fingerprint"),
                        summary.get("org_id"),
                        summary.get("alert_count", 0),
                        json.dumps(payload),
                    ),
                )
                row = await cur.fetchone()
                return int(row[0]) if row else None
    except Exception:  # pragma: no cover
        logger.exception("failed to persist incoming alert event")
        return None


async def save_loki_enrichment(event_id: int, enrichment: Dict[str, Any]) -> None:
    if db_pool() is None:
        return
    try:
        async with get_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE incoming_alert_events
                    SET loki_enrichment_json = %s::jsonb
                    WHERE id = %s;
                    """,
                    (json.dumps(enrichment), event_id),
                )
    except Exception:  # pragma: no cover
        logger.exception("failed to save loki enrichment event_id=%s", event_id)


async def fetch_event_by_id(event_id: int) -> Optional[Dict[str, Any]]:
    """Load persisted Grafana payload and enrichment for RCA rerun."""
    if db_pool() is None:
        raise RuntimeError("database pool not initialized")
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    id,
                    received_at,
                    source,
                    status,
                    alertname,
                    group_key,
                    fingerprint,
                    org_id,
                    alert_count,
                    payload_json,
                    loki_enrichment_json
                FROM incoming_alert_events
                WHERE id = %s;
                """,
                (event_id,),
            )
            row = await cur.fetchone()

    if not row:
        return None

    payload = row[9]
    enrichment = row[10]
    if not isinstance(payload, dict):
        return None

    return {
        "id": row[0],
        "received_at": row[1].isoformat() if row[1] else None,
        "source": row[2],
        "status": row[3],
        "alertname": row[4],
        "group_key": row[5],
        "fingerprint": row[6],
        "org_id": row[7],
        "alert_count": row[8],
        "payload_json": payload,
        "loki_enrichment": enrichment if isinstance(enrichment, dict) else None,
    }


async def fetch_recent_events(limit: int) -> list[Dict[str, Any]]:
    if db_pool() is None:
        raise RuntimeError("database pool not initialized")
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    id,
                    received_at,
                    source,
                    status,
                    alertname,
                    group_key,
                    fingerprint,
                    org_id,
                    alert_count,
                    loki_enrichment_json
                FROM incoming_alert_events
                ORDER BY received_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            rows = await cur.fetchall()

    events: list[Dict[str, Any]] = []
    for row in rows:
        enrichment = row[9]
        enrichment_out: Optional[Dict[str, Any]]
        if isinstance(enrichment, dict):
            enrichment_out = enrichment
        else:
            enrichment_out = None
        events.append(
            {
                "id": row[0],
                "received_at": row[1].isoformat() if row[1] else None,
                "source": row[2],
                "status": row[3],
                "alertname": row[4],
                "group_key": row[5],
                "fingerprint": row[6],
                "org_id": row[7],
                "alert_count": row[8],
                "loki_enrichment": enrichment_out,
            }
        )
    return events
