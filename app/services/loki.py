"""Loki query_range client and Grafana-alert enrichment."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_NONPROD_ENV_SLUGS = frozenset(
    {
        "nonprod",
        "non-prod",
        "non_prod",
        "staging",
        "stage",
        "dev",
        "development",
        "test",
        "qa",
        "uat",
    }
)


def resolve_loki_base_url(environment_slug: Optional[str]) -> Tuple[Optional[str], str]:
    """
    Pick which Loki instance to query.

    Prefer routing from Grafana alert labels (same signal as the firing rule).
    Falls back to LOKI_URL_DEFAULT, then prod, then non-prod if only one is set.
    """
    prod = settings.loki_url_prod or None
    nonprod = settings.loki_url_nonprod or None
    default = settings.loki_url_default or None

    if not environment_slug:
        chosen = default or prod or nonprod
        return chosen, "no_environment_label"

    e = environment_slug.strip().lower()

    if e in ("prod", "production"):
        return prod or default or nonprod, "mapped_prod"

    if e in _NONPROD_ENV_SLUGS:
        return nonprod or default or prod, "mapped_nonprod"

    if default:
        return default, "fallback_default"

    # Unknown label value with no default (above): prod/nonprod only if set.
    return prod or nonprod, "fallback_unknown_label"


_ENV_KEYS = ("loki_env", "loki_stack", "alloy_environment", "environment", "env", "stage")


def extract_environment_slug(labels: Dict[str, Any]) -> Optional[str]:
    """Read env-like routing from alert labels only (LogQL stream labels stay separate)."""
    if not labels:
        return None
    for key in _ENV_KEYS:
        raw = labels.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return None


def extract_environment_from_alert(first_alert: Dict[str, Any]) -> Optional[str]:
    """
    Env for **which Loki URL** to call: Grafana sends both `labels` and `annotations`.

    Prefer labels over annotations (same key order). Use optional **`loki_env`** / **`loki_stack`**
    on the alert rule if you want routing independent of log stream labels.
    """
    if not isinstance(first_alert, dict):
        return None
    labels = first_alert.get("labels") if isinstance(first_alert.get("labels"), dict) else {}
    annotations = first_alert.get("annotations") if isinstance(first_alert.get("annotations"), dict) else {}
    for source in (labels, annotations):
        for key in _ENV_KEYS:
            raw = source.get(key)
            if raw is not None and str(raw).strip():
                return str(raw).strip()
    return None


def _escape_logql_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _matchers_service_name_only(labels: Dict[str, Any]) -> List[str]:
    """Grafana Explore often uses `{service_name="$app"}` only — streams may omit `environment`."""
    svc = labels.get("service_name")
    if svc is None or not str(svc).strip():
        return []
    sv = _escape_logql_label_value(str(svc).strip())
    return [f'service_name="{sv}"']


def _matchers_core(labels: Dict[str, Any]) -> List[str]:
    """environment / alloy_environment + service_name (typical Loki stream identity)."""
    matchers: List[str] = []
    if labels.get("environment") is not None and str(labels.get("environment")).strip():
        ev = _escape_logql_label_value(str(labels.get("environment")).strip())
        matchers.append(f'environment="{ev}"')
    elif labels.get("alloy_environment") is not None and str(labels.get("alloy_environment")).strip():
        ev = _escape_logql_label_value(str(labels.get("alloy_environment")).strip())
        matchers.append(f'alloy_environment="{ev}"')

    svc = labels.get("service_name")
    if svc is not None and str(svc).strip():
        sv = _escape_logql_label_value(str(svc).strip())
        matchers.append(f'service_name="{sv}"')
    return matchers


def stream_selector_variants(labels: Dict[str, Any]) -> List[str]:
    """
    Ordered LogQL stream selectors to try: narrow first, then broader.

    Alert labels often differ slightly from Loki stream labels; dropping `instance` / `alloy_job`
    helps. If Explore uses `{service_name="..."}` only (no env on stream), we try **`service_name` alone** last.
    """
    if not labels:
        return []

    variants: List[str] = []
    seen: set[str] = set()

    def push(matchers: List[str]) -> None:
        if not matchers:
            return
        s = "{" + ", ".join(matchers) + "}"
        if s not in seen:
            seen.add(s)
            variants.append(s)

    core = _matchers_core(labels)
    inst_m: List[str] = []
    if labels.get("instance") is not None and str(labels.get("instance")).strip():
        iv = _escape_logql_label_value(str(labels.get("instance")).strip())
        inst_m = [f'instance="{iv}"']

    aj_m: List[str] = []
    if labels.get("alloy_job") is not None and str(labels.get("alloy_job")).strip():
        jv = _escape_logql_label_value(str(labels.get("alloy_job")).strip())
        aj_m = [f'alloy_job="{jv}"']

    if not core:
        job = labels.get("job") or labels.get("alloy_job")
        if job is not None and str(job).strip():
            jv = _escape_logql_label_value(str(job).strip())
            if labels.get("job") is not None and str(labels.get("job")).strip():
                push([f'job="{jv}"'])
            else:
                push([f'alloy_job="{jv}"'])
        return variants

    # Order: full → drop alloy_job → drop instance → env+service only (deduped).
    matcher_lists: List[List[str]] = [
        core + inst_m + aj_m,
        core + inst_m,
        core + aj_m,
        core,
    ]
    seen_m: Set[Tuple[str, ...]] = set()
    for m in matcher_lists:
        t = tuple(m)
        if not m or t in seen_m:
            continue
        seen_m.add(t)
        push(m)

    svc_only = _matchers_service_name_only(labels)
    if svc_only:
        t = tuple(svc_only)
        if t not in seen_m:
            seen_m.add(t)
            push(svc_only)

    # Grafana alert rules often add both `environment` and `alloy_environment`; `_matchers_core` uses
    # `environment` first. Many Loki streams only retain `alloy_environment` — try that ladder too.
    if labels.get("environment") and labels.get("alloy_environment"):
        alt_labels = {k: v for k, v in labels.items() if k != "environment"}
        for sel in stream_selector_variants(alt_labels):
            if sel not in seen:
                seen.add(sel)
                variants.append(sel)

    # Alloy/Docker often index service_name without a leading "/" — alert labels may include it.
    svc_raw = labels.get("service_name")
    if svc_raw is not None and str(svc_raw).strip().startswith("/"):
        alt_labels = dict(labels)
        alt_labels["service_name"] = str(svc_raw).strip().lstrip("/")
        for sel in stream_selector_variants(alt_labels):
            if sel not in seen:
                seen.add(sel)
                variants.append(sel)

    return variants


def labels_to_stream_selector(labels: Dict[str, Any]) -> Optional[str]:
    """Single preferred selector (first variant)."""
    v = stream_selector_variants(labels)
    return v[0] if v else None


def _append_logql_line_regex(stream_selector: str, regex_body: str) -> str:
    """Append LogQL line filter ``|~ "..."`` (regex_body uses RE2 / Grafana-syntax escapes)."""
    escaped = regex_body.replace("\\", "\\\\").replace('"', '\\"')
    return f'{stream_selector} |~ "{escaped}"'


def error_focused_selector_variants(
    selectors: List[str],
    regex_body: str,
) -> List[str]:
    """Same selector ladder as the broad query, each with an error-oriented ``|~`` pipeline."""
    if not regex_body.strip():
        return []
    out: List[str] = []
    seen: set[str] = set()
    for s in selectors:
        q = _append_logql_line_regex(s, regex_body.strip())
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


def _merge_loki_raw_results(parts: List[Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    """Combine raw Loki ``query_range`` payloads (dedupe by stream labels + index ts + line text)."""
    buckets: Dict[str, Dict[str, Any]] = {}
    for part in parts:
        if not part or not isinstance(part.get("result"), list):
            continue
        for stream in part["result"]:
            if not isinstance(stream, dict):
                continue
            labels = stream.get("stream") if isinstance(stream.get("stream"), dict) else {}
            lk = json.dumps(labels, sort_keys=True, default=str)
            if lk not in buckets:
                buckets[lk] = {"stream": labels, "_seen": set(), "values": []}
            bucket = buckets[lk]
            seen: Set[Tuple[str, str]] = bucket["_seen"]
            for pair in stream.get("values") if isinstance(stream.get("values"), list) else []:
                if isinstance(pair, list) and len(pair) >= 2:
                    ts_s = str(pair[0])
                    line_s = str(pair[1])
                    key = (ts_s, line_s)
                    if key not in seen:
                        seen.add(key)
                        bucket["values"].append([ts_s, line_s])
    result: List[Dict[str, Any]] = []
    for lk, bucket in buckets.items():
        vals = bucket["values"]
        vals.sort(key=lambda p: int(p[0]) if str(p[0]).isdigit() else 0)
        result.append({"stream": bucket["stream"], "values": vals})
    return {"resultType": "streams", "result": result}


def _loki_result_has_streams(data: Optional[Dict[str, Any]]) -> bool:
    if not data or not isinstance(data, dict):
        return False
    for stream in data.get("result") if isinstance(data.get("result"), list) else []:
        if not isinstance(stream, dict):
            continue
        values = stream.get("values") if isinstance(stream.get("values"), list) else []
        if values:
            return True
    return False


def _parse_rfc3339_time(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _window_around_starts_at(starts_at: datetime, now: datetime) -> Tuple[datetime, datetime]:
    """
    Asymmetric window around Grafana ``startsAt``.

    - Usually **end** = ``min(now, startsAt + after)`` so backward queries do not fill samples
      with logs far past the incident up to wall-clock **now**.
    - If **now < startsAt** (agent clock lags Grafana/Loki or manual webhook timing), **do not**
      cap **end** at **now** — that would query *before* the incident and return the wrong hour
      entirely. Use **startsAt + after** instead.

    Loki ``query_range`` with **backward** returns **newest first** in the range.
    """
    before = timedelta(minutes=max(1, settings.loki_enrich_window_minutes))
    after_m = max(0, settings.loki_enrich_after_start_minutes)
    after = timedelta(minutes=after_m)
    start = starts_at - before
    if now >= starts_at:
        end = min(now, starts_at + after)
    else:
        end = starts_at + after
    if end <= start:
        end = start + timedelta(minutes=1)
    return start, end


def _window_span_skew_bridging(
    starts_at: datetime,
    now: datetime,
    delta: timedelta,
    after_td: timedelta,
) -> Tuple[datetime, datetime]:
    """
    Wide bridge window when agent clock and Grafana ``startsAt`` disagree: from
    ``min(now,startsAt)-delta`` to ``min(max(now,startsAt)+delta, startsAt+after)``.
    Used only as a **retry** after the tight ``startsAt`` window returns no streams.
    """
    earliest = min(now, starts_at) - delta
    latest = min(max(now, starts_at) + delta, starts_at + after_td)
    if latest <= earliest:
        latest = earliest + timedelta(minutes=1)
    return earliest, latest


async def _enrichment_query_pass(
    base_url: str,
    selectors: List[str],
    window_start: datetime,
    window_end: datetime,
    env_slug: str,
    attempts: List[Dict[str, Any]],
    *,
    pass_label: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[Dict[str, Any]]]:
    """
    Try each LogQL selector once for the given window. Appends to ``attempts`` and returns
    (chosen_data, chosen_query, last_ok_data) with **raw** Loki ``data`` (line caps applied later
    in :func:`_normalize_loki_result` after optional error-pass merge).
    """
    chosen_data: Optional[Dict[str, Any]] = None
    chosen_query: Optional[str] = None
    last_ok_data: Optional[Dict[str, Any]] = None

    for logql in selectors:
        try:
            qr = await query_range(base_url, logql, window_start, window_end)
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "loki query_range HTTP error env=%s query=%s status=%s",
                env_slug,
                logql,
                exc.response.status_code if exc.response is not None else None,
            )
            attempts.append(
                {
                    "pass": pass_label,
                    "query": logql,
                    "error": "http_error",
                    "http_status": exc.response.status_code if exc.response is not None else None,
                }
            )
            continue
        except Exception as exc:  # pragma: no cover
            logger.warning("loki query_range failed env=%s query=%s err=%s", env_slug, logql, exc)
            attempts.append({"pass": pass_label, "query": logql, "error": type(exc).__name__, "detail": str(exc)})
            continue

        if qr.get("error"):
            attempts.append({"pass": pass_label, "query": logql, "error": qr.get("error")})
            continue

        data = qr.get("data")
        if not isinstance(data, dict):
            attempts.append({"pass": pass_label, "query": logql, "error": "missing_data"})
            continue

        streams = data.get("result") if isinstance(data.get("result"), list) else []
        line_count = sum(
            len(s.get("values") or []) for s in streams if isinstance(s, dict)
        )
        attempts.append(
            {"pass": pass_label, "query": logql, "streams": len(streams), "lines": line_count}
        )
        last_ok_data = data

        if _loki_result_has_streams(data):
            chosen_data = data
            chosen_query = logql
            break

    return chosen_data, chosen_query, last_ok_data


def alert_time_window(
    first_alert: Dict[str, Any],
) -> Tuple[datetime, datetime, Optional[str]]:
    """
    **alert_start** (default): query Loki by **index timestamp** aligned to Grafana ``startsAt``
    (not text inside log lines). Primary window is **tight**: ``startsAt − before …`` capped after
    ``startsAt`` (see ``_window_around_starts_at``). If ``startsAt`` is ahead of agent clock and the
    gap is within ``LOKI_ENRICH_FUTURE_GAP_SPAN_MAX_MINUTES``, enrichment may **retry** with a
    wider bridge window only when the tight query returns no streams (see ``enrich_grafana_alert``).

    **now**: symmetric ±window around ingestion time (``LOKI_ENRICH_ANCHOR=now``).
    """
    now = datetime.now(timezone.utc)
    delta = timedelta(minutes=max(1, settings.loki_enrich_window_minutes))
    after_td = timedelta(minutes=max(0, settings.loki_enrich_after_start_minutes))

    if settings.loki_enrich_anchor == "now":
        center = now
        return center - delta, center + delta, "anchor_config_now"

    starts_at = _parse_rfc3339_time(first_alert.get("startsAt"))
    if starts_at is None:
        center = now
        return center - delta, center + delta, "anchor_now_unparsable_startsAt"

    if starts_at > now + timedelta(minutes=2):
        gap_minutes = (starts_at - now).total_seconds() / 60.0
        max_gap = max(1, settings.loki_enrich_future_gap_span_max_minutes)
        if settings.loki_enrich_clamp_future_starts_at:
            # Prefer tight window around Grafana startsAt (Loki TS ↔ alert). Wide span is a retry in enrich.
            if gap_minutes <= max_gap:
                start, end = _window_around_starts_at(starts_at, now)
                return start, end, "anchor_startsAt_skew_tight"
            # Gap wider than cap: still anchor on Grafana startsAt — NOT on agent "now". The old
            # behavior (±window around now) broke when now << startsAt (skew): we queried the wrong hour.
            start, end = _window_around_starts_at(starts_at, now)
            return start, end, "anchor_startsAt_future_gap_wide_use_startsAt_not_agent_now"
        start, end = _window_around_starts_at(starts_at, now)
        return start, end, "anchor_alert_start_future_unclamped"

    start, end = _window_around_starts_at(starts_at, now)
    return start, end, "anchor_asymmetric_after_start"


def _normalize_loki_result(
    data: Dict[str, Any],
    *,
    anchor_dt: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Shrink Loki query_range JSON for Postgres storage.

    When ``anchor_dt`` is set (Grafana ``startsAt``), prefer lines whose **Loki timestamps** are
    closest to that instant — ``direction=backward`` alone returns newest-first and can fill the
    cap with traffic **after** the alert moment within the same window.

    If :attr:`Settings.loki_enrich_stratify_lines` is true, the line cap is split: a **before** budget
    (index time ``<`` ``startsAt``) and an **after** budget, so lead-up and post-incident lines are
    both represented; unused slots spill to the other side.
    """
    result = data.get("result") if isinstance(data.get("result"), list) else []
    streams_in = result[: settings.loki_result_max_streams]
    max_lines = settings.loki_result_max_total_lines

    if anchor_dt is None or not isinstance(anchor_dt, datetime):
        streams_out: List[Dict[str, Any]] = []
        lines_left = max_lines
        for stream in streams_in:
            if lines_left <= 0:
                break
            stream_labels = stream.get("stream") if isinstance(stream.get("stream"), dict) else {}
            values = stream.get("values") if isinstance(stream.get("values"), list) else []
            chunk: List[List[str]] = []
            for pair in values:
                if lines_left <= 0:
                    break
                if isinstance(pair, list) and len(pair) >= 2:
                    chunk.append([str(pair[0]), str(pair[1])])
                    lines_left -= 1
            streams_out.append({"stream": stream_labels, "values": chunk})
        return {
            "resultType": data.get("resultType"),
            "streams_truncated": len(result) > len(streams_in),
            "lines_truncated": lines_left <= 0,
            "result": streams_out,
        }

    anchor_ns = int(anchor_dt.timestamp() * 1_000_000_000)
    flat: List[Tuple[int, int, str, str, str]] = []  # dist, ts_ns, ts_str, line, label_key
    label_key_to_labels: Dict[str, Dict[str, Any]] = {}
    total_in = 0
    for stream in streams_in:
        if not isinstance(stream, dict):
            continue
        stream_labels = stream.get("stream") if isinstance(stream.get("stream"), dict) else {}
        label_key = json.dumps(stream_labels, sort_keys=True, default=str)
        label_key_to_labels[label_key] = stream_labels
        for pair in stream.get("values") if isinstance(stream.get("values"), list) else []:
            if isinstance(pair, list) and len(pair) >= 2:
                total_in += 1
                ts_str = str(pair[0])
                try:
                    ts_ns = int(ts_str)
                except ValueError:
                    continue
                dist = abs(ts_ns - anchor_ns)
                flat.append((dist, ts_ns, ts_str, str(pair[1]), label_key))

    use_stratify = (
        settings.loki_enrich_stratify_lines
        and settings.loki_enrich_prioritize_starts_at
        and isinstance(anchor_dt, datetime)
    )
    if use_stratify:
        before_lines = [x for x in flat if x[1] < anchor_ns]
        after_lines = [x for x in flat if x[1] >= anchor_ns]
        before_lines.sort(key=lambda x: x[0])
        after_lines.sort(key=lambda x: x[0])
        bp = settings.loki_enrich_line_budget_before_pct
        b_budget = max_lines * bp // 100
        a_budget = max_lines - b_budget
        kept_before = before_lines[:b_budget]
        kept_after = after_lines[:a_budget]
        merged_kept: List[Tuple[int, int, str, str, str]] = kept_before + kept_after
        if len(merged_kept) < max_lines:
            idx_b = len(kept_before)
            idx_a = len(kept_after)
            while len(merged_kept) < max_lines:
                if idx_b < len(before_lines):
                    merged_kept.append(before_lines[idx_b])
                    idx_b += 1
                elif idx_a < len(after_lines):
                    merged_kept.append(after_lines[idx_a])
                    idx_a += 1
                else:
                    break
        kept = merged_kept[:max_lines]
    else:
        flat.sort(key=lambda x: x[0])
        kept = flat[:max_lines]
    lines_truncated = total_in > max_lines

    buckets: Dict[str, List[List[str]]] = defaultdict(list)
    for _dist, _ts_ns, ts_str, line, lk in kept:
        buckets[lk].append([ts_str, line])

    streams_out_prio: List[Dict[str, Any]] = []
    for lk, pairs in buckets.items():
        pairs.sort(key=lambda p: int(p[0]) if str(p[0]).isdigit() else 0)
        streams_out_prio.append({"stream": label_key_to_labels.get(lk, {}), "values": pairs})

    streams_out_prio.sort(
        key=lambda s: min(int(v[0]) for v in s["values"]) if s.get("values") else 0
    )

    return {
        "resultType": data.get("resultType"),
        "streams_truncated": len(result) > len(streams_in),
        "lines_truncated": lines_truncated,
        "result": streams_out_prio,
    }


async def query_range(
    base_url: str,
    query: str,
    start: datetime,
    end: datetime,
) -> Dict[str, Any]:
    url = urljoin(base_url.rstrip("/") + "/", "loki/api/v1/query_range")
    start_ns = int(start.timestamp() * 1_000_000_000)
    end_ns = int(end.timestamp() * 1_000_000_000)
    headers: Dict[str, str] = {}
    if settings.loki_bearer_token:
        headers["Authorization"] = f"Bearer {settings.loki_bearer_token}"
    if settings.loki_scope_org_id:
        headers["X-Scope-OrgID"] = settings.loki_scope_org_id

    params = {
        "query": query,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": str(settings.loki_query_limit),
        "direction": settings.loki_query_direction,
    }

    timeout = httpx.Timeout(settings.loki_query_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            return {"error": "unexpected_loki_response_shape", "raw_type": type(body).__name__}
        if body.get("status") != "success":
            return {"error": "loki_status_not_success", "body": body}
        data = body.get("data")
        if not isinstance(data, dict):
            return {"error": "missing_data", "body": body}
        return {"ok": True, "data": data}


async def enrich_grafana_alert(
    payload: Dict[str, Any],
    summary: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Run query_range against the Loki instance chosen from alert environment labels.

    Returns a JSON-serializable dict stored on `incoming_alert_events.loki_enrichment_json`.
    """
    if not settings.loki_enrich_enabled:
        return {"skipped": True, "reason": "loki_enrich_disabled"}

    if not (settings.loki_url_prod or settings.loki_url_nonprod or settings.loki_url_default):
        return {"skipped": True, "reason": "no_loki_urls_configured"}

    alerts = payload.get("alerts") if isinstance(payload.get("alerts"), list) else []
    first_alert = alerts[0] if alerts and isinstance(alerts[0], dict) else {}
    labels = first_alert.get("labels") if isinstance(first_alert.get("labels"), dict) else {}

    env_from_summary = summary.get("environment")
    env_slug = (
        env_from_summary
        if isinstance(env_from_summary, str) and env_from_summary.strip()
        else extract_environment_from_alert(first_alert)
    )
    base_url, routing_reason = resolve_loki_base_url(env_slug)

    if not base_url:
        return {
            "skipped": True,
            "reason": "no_loki_url_for_environment",
            "environment": env_slug,
            "routing_reason": routing_reason,
        }

    selectors = stream_selector_variants(labels)
    if not selectors:
        return {
            "skipped": True,
            "reason": "insufficient_labels_for_logql_selector",
            "environment": env_slug,
            "loki_base_url": base_url,
            "routing_reason": routing_reason,
        }

    primary_start, primary_end, window_anchor_note = alert_time_window(first_alert)
    window_start, window_end = primary_start, primary_end

    loki_anchor: Optional[datetime] = None
    if settings.loki_enrich_prioritize_starts_at:
        loki_anchor = _parse_rfc3339_time(first_alert.get("startsAt"))

    attempts: List[Dict[str, Any]] = []
    chosen_raw, chosen_query, last_ok_data = await _enrichment_query_pass(
        base_url,
        selectors,
        window_start,
        window_end,
        env_slug,
        attempts,
        pass_label="primary",
    )

    used_span_skew = False
    if (
        chosen_raw is None
        and settings.loki_enrich_retry_wide_span_on_empty
        and settings.loki_enrich_anchor != "now"
        and settings.loki_enrich_clamp_future_starts_at
    ):
        starts_span = _parse_rfc3339_time(first_alert.get("startsAt"))
        agent_now = datetime.now(timezone.utc)
        if starts_span is not None and starts_span > agent_now + timedelta(minutes=2):
            gap_minutes = (starts_span - agent_now).total_seconds() / 60.0
            max_gap = max(1, settings.loki_enrich_future_gap_span_max_minutes)
            if gap_minutes <= max_gap:
                delta_td = timedelta(minutes=max(1, settings.loki_enrich_window_minutes))
                after_td = timedelta(minutes=max(0, settings.loki_enrich_after_start_minutes))
                span_s, span_e = _window_span_skew_bridging(starts_span, agent_now, delta_td, after_td)
                logger.info(
                    "loki enrichment: tight startsAt window returned no streams; retry wide span env=%s",
                    env_slug,
                )
                sd, sq, sl = await _enrichment_query_pass(
                    base_url,
                    selectors,
                    span_s,
                    span_e,
                    env_slug,
                    attempts,
                    pass_label="span_skew",
                )
                if sd is not None and sq is not None:
                    chosen_raw = sd
                    chosen_query = sq
                    last_ok_data = sl
                    window_start, window_end = span_s, span_e
                    used_span_skew = True
                    extra = "loki_retry_wide_span_after_empty_tight"
                    window_anchor_note = f"{window_anchor_note}; {extra}" if window_anchor_note else extra

    used_fallback_now = False
    if (
        chosen_raw is None
        and settings.loki_enrich_empty_fallback_to_now
        and settings.loki_enrich_anchor != "now"
    ):
        starts_chk = _parse_rfc3339_time(first_alert.get("startsAt"))
        agent_now = datetime.now(timezone.utc)
        if starts_chk is not None and starts_chk > agent_now + timedelta(minutes=2):
            # Symmetric ±window around agent *now* alone can miss the incident entirely when
            # Grafana's startsAt is far ahead of the container clock (common with NTP skew or
            # long gap > LOKI_ENRICH_FUTURE_GAP_SPAN_MAX_MINUTES, where the wide span_skew retry
            # is skipped). Bridge [min(now,startsAt)−w .. max(now,startsAt)+w] capped at
            # startsAt+after — same shape as ``span_skew`` — so Loki still samples around the
            # alert time, not only "now" on a lagging host.
            delta_fb = timedelta(minutes=max(1, settings.loki_enrich_window_minutes))
            after_fb = timedelta(minutes=max(0, settings.loki_enrich_after_start_minutes))
            fb_start, fb_end = _window_span_skew_bridging(starts_chk, agent_now, delta_fb, after_fb)
            logger.info(
                "loki enrichment: empty after primary/retries, startsAt ahead of agent clock; "
                "retrying with bridge window (agent now + Grafana startsAt) env=%s",
                env_slug,
            )
            fb_data, fb_query, fb_last = await _enrichment_query_pass(
                base_url,
                selectors,
                fb_start,
                fb_end,
                env_slug,
                attempts,
                pass_label="fallback_now",
            )
            if fb_data is not None and fb_query is not None:
                chosen_raw = fb_data
                chosen_query = fb_query
                last_ok_data = fb_last
                window_start, window_end = fb_start, fb_end
                used_fallback_now = True
                extra = "loki_empty_fallback_bridge_now_and_startsAt"
                window_anchor_note = f"{window_anchor_note}; {extra}" if window_anchor_note else extra

    base_meta: Dict[str, Any] = {
        "skipped": False,
        "environment": env_slug,
        "loki_base_url": base_url,
        "routing_reason": routing_reason,
        "window": {
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
        },
        "selector_attempts": attempts,
    }
    if loki_anchor is not None:
        base_meta["line_selection_anchor"] = loki_anchor.isoformat()
    if window_anchor_note:
        base_meta["window_anchor_note"] = window_anchor_note
    if used_fallback_now or used_span_skew:
        base_meta["window_primary_attempt"] = {
            "start": primary_start.isoformat(),
            "end": primary_end.isoformat(),
        }
    base_meta["query_direction"] = settings.loki_query_direction
    if settings.loki_enrich_stratify_lines and loki_anchor is not None:
        base_meta["line_budget_before_pct"] = settings.loki_enrich_line_budget_before_pct

    error_focus_query: Optional[str] = None
    if (
        settings.loki_enrich_error_pass_enabled
        and settings.loki_enrich_error_logql_regex.strip()
        and chosen_raw is not None
        and _loki_result_has_streams(chosen_raw)
        and chosen_query is not None
    ):
        # Reuse only the selector that matched the broad pass; trying the full ladder again with
        # `|~` would issue N more queries to Loki (was 18 in one observed run) without any benefit —
        # adjacent label variants describe streams that aren't this alert's service.
        err_sel = error_focused_selector_variants([chosen_query], settings.loki_enrich_error_logql_regex)
        if err_sel:
            er_raw, eq, _ = await _enrichment_query_pass(
                base_url,
                err_sel,
                window_start,
                window_end,
                env_slug,
                attempts,
                pass_label="error_focused",
            )
            if er_raw is not None and _loki_result_has_streams(er_raw):
                chosen_raw = _merge_loki_raw_results([chosen_raw, er_raw])
                error_focus_query = eq

    if chosen_raw is not None and chosen_query is not None:
        out: Dict[str, Any] = {
            **base_meta,
            "query": chosen_query,
            "data": _normalize_loki_result(chosen_raw, anchor_dt=loki_anchor),
            "selector_fallback_used": chosen_query != selectors[0],
        }
        if error_focus_query is not None:
            out["error_focus_query"] = error_focus_query
        return out

    empty_stub = {
        "result": [],
        "resultType": "streams",
        "lines_truncated": False,
        "streams_truncated": False,
    }
    raw_fallback = last_ok_data if last_ok_data is not None else empty_stub
    final_data = _normalize_loki_result(raw_fallback, anchor_dt=loki_anchor)
    logger.info(
        "loki enrichment: no log lines in window after %d selector(s) env=%s; "
        "verify same Loki as Grafana Explore, label values, LOKI_SCOPE_ORG_ID, or widen LOKI_ENRICH_WINDOW_MINUTES",
        len(selectors),
        env_slug,
    )
    return {
        **base_meta,
        "query": selectors[0],
        "data": final_data,
        "note": "no_log_streams_matched_any_selector_or_window; confirm Explore (same Loki URL/tenant), label values, LOKI_SCOPE_ORG_ID, service_name slash, or widen LOKI_ENRICH_WINDOW_MINUTES",
    }

