"""Heuristic operator suggestions from alert summary + Loki enrichment (no LLM)."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# CSI (e.g. \x1b[31m … \x1b[39m, \x1b[38;5;3m) and OSC bell-terminated sequences
_CSI_RE = re.compile(r"\x1b\[[0-?]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07]*\x07")

# Match HTTP 429 as a response status — not bare "429" (matches timestamps, line numbers, etags).
_HTTP_429_RESPONSE_RE = re.compile(
    r'"(?:statusCode|status)"\s*:\s*429|\bHTTP/\d(?:\.\d)?\s+429\b|\s429\s+(?:Too\s+Many|too\s+many)\b',
    re.IGNORECASE,
)


def strip_ansi_escapes(text: str) -> str:
    """Remove terminal color / cursor ANSI codes from log lines (NestJS / Docker color output)."""
    if not text:
        return ""
    s = _OSC_RE.sub("", _CSI_RE.sub("", text))
    return s.replace("\u001b", "")


def enrichment_log_lines(enrichment: Optional[Dict[str, Any]]) -> List[str]:
    """Plain log line texts from stored Loki enrichment."""
    if not enrichment or enrichment.get("skipped"):
        return []
    return _iter_log_lines(enrichment)


# Loki enrichment skip reasons where generic LOKI_* / service_name bullets add noise, not signal.
_SMART_SKIP_SKIPPED_REASONS = frozenset(
    {
        "insufficient_labels_for_logql_selector",
        "no_loki_urls_configured",
        "no_loki_url_for_environment",
        "loki_enrich_disabled",
    }
)

_CHECKLIST_SKIP_MESSAGES: Dict[str, str] = {
    "grafana_no_data_alert": (
        "This alert name usually means Grafana evaluated the rule as **no series / no data** "
        "(datasource connectivity, query, or timing)—not a confirmed app failure from logs. "
        "Generic LogQL label hints are omitted; confirm the datasource and rule in Grafana if there is impact."
    ),
    "insufficient_labels_for_logql_selector": (
        "Alert labels cannot build a LogQL stream selector, so log-based hints would be guesses. "
        "Add `service_name` / env labels that match Explore, or tune notification routing—checklist omitted."
    ),
    "no_loki_urls_configured": (
        "This agent has **no Loki base URL** configured (`LOKI_URL_*`). "
        "Fix configuration before using log-driven suggestions—checklist omitted."
    ),
    "no_loki_url_for_environment": (
        "No Loki URL is mapped for this alert’s environment label. "
        "Set `LOKI_URL_DEFAULT` or matching prod/nonprod URLs—checklist omitted."
    ),
    "loki_enrich_disabled": (
        "Loki enrichment is **disabled** on this agent (`LOKI_ENRICH_ENABLED`). "
        "Turn it on if you expect log context—checklist omitted."
    ),
    "loki_query_transport_failure": (
        "Every Loki **query_range** attempt failed (network, HTTP, or timeout). "
        "Verify Loki reachability, TLS, bearer token, and `LOKI_SCOPE_ORG_ID` before tuning LogQL—generic checklist omitted."
    ),
}


def _alertname_implies_grafana_no_data(alertname: Optional[str]) -> bool:
    if not alertname or not isinstance(alertname, str):
        return False
    compact = alertname.strip().lower().replace(" ", "").replace("_", "").replace("-", "")
    if "datasourcenodata" in compact:
        return True
    low = alertname.strip().lower()
    return "datasource" in low and "no data" in low


def _selector_attempts_all_errors(enrichment: Dict[str, Any]) -> bool:
    """True when every recorded selector attempt failed before a successful Loki JSON body."""
    attempts = enrichment.get("selector_attempts")
    if not isinstance(attempts, list) or not attempts:
        return False
    dict_items = [x for x in attempts if isinstance(x, dict)]
    if not dict_items:
        return False
    for item in dict_items:
        if "streams" in item or "lines" in item:
            return False
        if "error" not in item:
            return False
    return True


def should_skip_operator_checklist(
    summary: Dict[str, Any],
    enrichment: Optional[Dict[str, Any]],
    *,
    enabled: bool = True,
) -> tuple[bool, str]:
    """
    Detect low-signal Grafana/Loki deliveries where generic operator bullets would mislead
    (DatasourceNoData, missing selectors, total Loki transport failure).

    Returns ``(True, reason_code)`` to replace the checklist with a short explanation, or ``(False, "")``.
    """
    if not enabled:
        return False, ""

    if _alertname_implies_grafana_no_data(summary.get("alertname")):
        return True, "grafana_no_data_alert"

    if enrichment is None:
        return False, ""

    if enrichment.get("skipped"):
        reason = str(enrichment.get("reason") or "")
        if reason in _SMART_SKIP_SKIPPED_REASONS:
            return True, reason
        return False, ""

    if _selector_attempts_all_errors(enrichment):
        return True, "loki_query_transport_failure"

    return False, ""


def checklist_skip_explainer(reason_code: str) -> str:
    """Single Slack-safe paragraph when :func:`should_skip_operator_checklist` fires."""
    return _CHECKLIST_SKIP_MESSAGES.get(
        reason_code,
        "Low-signal delivery for automated log hints—generic checklist omitted.",
    )


def _iter_log_lines(enrichment: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    if enrichment.get("skipped"):
        return lines
    data = enrichment.get("data") if isinstance(enrichment.get("data"), dict) else {}
    for stream in data.get("result") if isinstance(data.get("result"), list) else []:
        if not isinstance(stream, dict):
            continue
        for pair in stream.get("values") if isinstance(stream.get("values"), list) else []:
            if isinstance(pair, list) and len(pair) >= 2:
                lines.append(strip_ansi_escapes(str(pair[1])))
    return lines


def build_operator_suggestions(
    summary: Dict[str, Any],
    enrichment: Optional[Dict[str, Any]],
) -> List[str]:
    """Short bullets for Slack / humans; extend later with LLM output."""
    sug: List[str] = []
    if enrichment is None:
        return [
            "Loki enrichment did not run (no DB row or enrichment disabled)—fix Postgres / LOKI_* if you expected context.",
            "Once enrichment runs, suggestions will use log patterns (Telegram, 429, etc.).",
        ]

    lines: List[str] = []
    if not enrichment.get("skipped"):
        lines = _iter_log_lines(enrichment)

    blob = "\n".join(lines).lower()

    if enrichment.get("skipped"):
        reason = enrichment.get("reason", "unknown")
        sug.append(f"Loki enrichment skipped ({reason}). Check LOKI_* env and alert labels vs Explore.")
        sug.append("Confirm `service_name` matches Loki streams (including leading `/` if present).")
        return sug[:10]

    if not lines:
        sug.append("No log lines returned in the enrichment window—widen LOKI_ENRICH_WINDOW_MINUTES or check clock/skew.")
    else:
        if "error" in blob:
            sug.append(
                "Error-level log lines present—triage the newest stack traces and ERROR-level lines first "
                "(framework varies by service)."
            )
        if "telegram" in blob and ("error" in blob or "axios" in blob):
            sug.append("Telegram-related failures—verify bot token, chat permissions, and Telegram API reachability.")
        if _HTTP_429_RESPONSE_RE.search(blob):
            sug.append("HTTP 429 seen—likely rate limiting on an external API; backoff, caching, or alternate provider.")
        if "vpn" in blob and ("warn" in blob or _HTTP_429_RESPONSE_RE.search(blob)):
            sug.append("VPN/geo lookup warnings—external IP API may be rate-limited; consider caching or failover.")
        if "withdraw" in blob or "platform request" in blob:
            sug.append("Wallet/platform request processing appears in logs—correlate failing requests by `requestId` / `webhookRequestId`.")

    sug.append(
        f"Window: {enrichment.get('query', 'n/a')[:120]}{'…' if len(str(enrichment.get('query', ''))) > 120 else ''}"
    )
    sug.append("Mute or tune Grafana rules if this noise is expected; escalate if customer impact.")

    # Dedupe while preserving order
    seen: set[str] = set()
    out: List[str] = []
    for s in sug:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out[:10]


_RCA_SECTION_TITLES = ("Likely cause", "Evidence", "Next steps", "Additional context")

# Strip "**RCA**", "**Root Cause Analysis**", "RCA:" etc. when the model adds them as a top-line
# preamble — the Slack header already says "RCA (Ollama — model)" so it is redundant, and Slack
# mrkdwn renders ``**...**`` as literal asterisks.
_RCA_PREAMBLE_RE = re.compile(
    r"^\s*\*{1,3}\s*(?:RCA|Root[\s-]?Cause(?:\s+Analysis)?)\s*\*{1,3}\s*:?\s*\n+",
    flags=re.IGNORECASE,
)


def _collapse_section_title_asterisks(text: str) -> str:
    """
    Slack mrkdwn only renders bold for ``*single*`` asterisks. Models often emit ``**Title**`` or
    ``***Title***`` (markdown bold / bold-italic), which Slack shows as literal asterisks. Replace
    any 1+/2+/3+ asterisks wrapping the canonical RCA section titles with exactly ``*Title*`` and
    ensure a blank line follows so the next sentence does not glue to the heading.

    Also strip a redundant leading ``**RCA**`` / ``**Root Cause Analysis**`` preamble.
    """
    if not text:
        return text
    text = _RCA_PREAMBLE_RE.sub("", text)
    pattern = re.compile(
        r"\*{1,3}\s*(" + "|".join(re.escape(t) for t in _RCA_SECTION_TITLES) + r")\s*\*{1,3}",
        flags=re.IGNORECASE,
    )

    def _repl(match: "re.Match[str]") -> str:
        title = match.group(1)
        return f"*{title}*\n\n"

    out = pattern.sub(_repl, text)
    out = re.sub(r"\n\n[ \t]+", "\n\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.lstrip()


def _ensure_blank_line_before_rca_sections(text: str) -> str:
    """
    Models often glue ``*Evidence*`` / ``*Next steps*`` onto the end of the previous paragraph on the
    same physical line (Slack shows ``... alert. Evidence`` with Evidence not starting a new block).
    Force ``\\n\\n`` before each canonical section title when it is only separated by spaces or a
    single newline from preceding prose.
    """
    if not text:
        return text
    out = text
    for raw_title in _RCA_SECTION_TITLES:
        wrapped = "*" + raw_title + "*"
        w = re.escape(wrapped)
        # "... sentence. *Evidence*" same line
        out = re.sub(rf"([.!?])\s+({w})", r"\1\n\n\2", out, flags=re.IGNORECASE)
        # "... sentence.\n*Evidence*" single newline — need blank line before heading
        out = re.sub(rf"([.!?])\n({w})", r"\1\n\n\2", out, flags=re.IGNORECASE)
        # word boundary without sentence end (rare): "... keywords *Evidence*"
        out = re.sub(rf"(\S)\s+({w})", r"\1\n\n\2", out, flags=re.IGNORECASE)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def redact_for_slack(text: str, max_len: int = 2800) -> str:
    """Strip presigned URLs and trim for Slack limits."""
    s = re.sub(r"https?://[^\s<>]+", "[url]", text)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s


_SNIPPET_HOT_LINE = re.compile(
    r'(?:"level"\s*:\s*"(?:ERROR|WARN|error|warn|fatal|FATAL)")'
    r"|request errored|simulated error path|\"msg\"\s*:\s*\"[^\"]*(?:exception|panic)",
    re.IGNORECASE,
)


def prioritize_log_lines_for_snippets(lines: List[str], *, max_take: int = 32) -> List[str]:
    """
    Slack snippet budget is tiny (see :func:`format_log_snippets`). Put ERROR/WARN-looking lines first
    so the digest is not five identical ``/health`` INFO lines when an ERROR exists in the same window.
    """
    if not lines:
        return []
    hot: List[str] = []
    cold: List[str] = []
    for ln in lines:
        if _SNIPPET_HOT_LINE.search(ln):
            hot.append(ln)
        else:
            cold.append(ln)
    merged = hot + cold
    return merged[:max_take]


def format_log_snippets(lines: List[str], max_lines: int = 5, max_each: int = 240) -> str:
    """Pick a few lines after optional prioritization (caller may run :func:`prioritize_log_lines_for_snippets`)."""
    if not lines:
        return ""
    chunks: List[str] = []
    for raw in lines[:max_lines]:
        chunks.append(redact_for_slack(raw, max_len=max_each))
    return "\n".join(f"• `{c}`" for c in chunks if c)
