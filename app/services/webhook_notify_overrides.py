"""Per-request Slack / Ollama toggles for POST /hooks/grafana (no env redeploy)."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from starlette.datastructures import QueryParams

# Grafana alert rule: set as **annotation** or **custom label** on the rule.
_NOTIFY_SLACK_KEYS = ("ai_sre_notify_slack",)
_OLLAMA_RCA_KEYS = ("ai_sre_ollama_rca",)


def parse_optional_bool(raw: Any) -> Optional[bool]:
    """Return ``True`` / ``False`` for common truthy strings; ``None`` if absent or unrecognized."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return None


def _first_alert(payload: Dict[str, Any]) -> Dict[str, Any]:
    alerts = payload.get("alerts") if isinstance(payload.get("alerts"), list) else []
    if alerts and isinstance(alerts[0], dict):
        return alerts[0]
    return {}


def _first_truthy_ann_then_labels(first: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[bool]:
    """Annotations override labels when both set the same key (case-insensitive)."""
    ann = first.get("annotations") if isinstance(first.get("annotations"), dict) else {}
    lbl = first.get("labels") if isinstance(first.get("labels"), dict) else {}
    for source in (ann, lbl):
        lower_map = {str(k).lower(): v for k, v in source.items()}
        for key in keys:
            lk = key.lower()
            if lk in lower_map:
                b = parse_optional_bool(lower_map[lk])
                if b is not None:
                    return b
    return None


def _query_override(params: QueryParams, name: str) -> Optional[bool]:
    raw = params.get(name)
    if raw is None or raw == "":
        return None
    return parse_optional_bool(raw)


def resolve_grafana_webhook_notify_flags(
    payload: Dict[str, Any],
    query_params: QueryParams,
    *,
    settings_slack_notify: bool,
    settings_ollama_rca: bool,
) -> Tuple[bool, bool]:
    """
    Effective Slack post and Ollama RCA for this webhook request.

    Precedence (first hit wins): alert **annotation** → **label** → URL **query** → **settings**.

    Query params: ``slack_notify``, ``ollama_rca`` (``true`` / ``false`` / ``1`` / ``0`` / ``yes`` / ``no``).

    Alert metadata: ``ai_sre_notify_slack``, ``ai_sre_ollama_rca`` on annotations or labels.
    """
    first = _first_alert(payload)

    slack_v = _first_truthy_ann_then_labels(first, _NOTIFY_SLACK_KEYS)
    if slack_v is None:
        slack_v = _query_override(query_params, "slack_notify")
    effective_slack = settings_slack_notify if slack_v is None else slack_v

    ollama_v = _first_truthy_ann_then_labels(first, _OLLAMA_RCA_KEYS)
    if ollama_v is None:
        ollama_v = _query_override(query_params, "ollama_rca")
    effective_ollama = settings_ollama_rca if ollama_v is None else ollama_v

    return effective_slack, effective_ollama
