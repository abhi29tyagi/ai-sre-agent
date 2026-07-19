"""Application configuration loaded from environment variables."""

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_SLACK_HOOK_MARKER = "https://hooks.slack.com/services/"
# First segment after marker: T…/B…/token — reject token if it looks like a pasted URL (`http…`).
_SLACK_HOOK_PATH_RE = re.compile(
    r"^([T][A-Z0-9]+)/([B][A-Z0-9]+)/([A-Za-z0-9]+)",
    re.IGNORECASE,
)


def _normalize_slack_incoming_webhook_url(raw: str) -> str:
    """
    If SLACK_WEBHOOK_URL contains multiple pasted Incoming Webhook URLs (or one URL wrongly nested in another),
    keep valid candidates only and use the last one (nested-paste case).
    """
    s = raw.strip()
    if not s:
        return s
    candidates: list[str] = []
    for m in re.finditer(re.escape(_SLACK_HOOK_MARKER), s):
        rest = s[m.end() :]
        mm = _SLACK_HOOK_PATH_RE.match(rest)
        if not mm:
            continue
        team, bid, tok = mm.groups()
        if tok.lower().startswith("http"):
            continue
        candidates.append(f"{_SLACK_HOOK_MARKER}{team}/{bid}/{tok}")
    if not candidates:
        return s
    chosen = candidates[-1]
    if len(candidates) > 1:
        logger.warning(
            "SLACK_WEBHOOK_URL contained %s valid Incoming Webhook URLs; using only the last",
            len(candidates),
        )
    elif s != chosen:
        logger.warning(
            "SLACK_WEBHOOK_URL was malformed (nested URL or extra text); using extracted Incoming Webhook URL",
        )
    return chosen


@dataclass(frozen=True)
class Settings:
    webhook_secret: str
    database_url: str
    postgres_host: str
    postgres_port: int
    postgres_user: str
    postgres_password: str
    postgres_db: str
    db_connect_timeout_seconds: int
    db_pool_max_size: int
    log_level: str
    # Loki: two bases for prod vs non-prod; pick URL from Grafana alert labels (see README).
    loki_url_prod: str
    loki_url_nonprod: str
    loki_url_default: str
    loki_bearer_token: str
    # Multi-tenant Loki / Grafana Mimir-style: send X-Scope-OrgID on query_range.
    loki_scope_org_id: str
    loki_enrich_enabled: bool
    loki_enrich_window_minutes: int
    # alert_start: ±window around Grafana startsAt. now: ±window around ingestion time.
    loki_enrich_anchor: str
    # If startsAt > now+2m, anchor on now (avoid empty future window). Set false to always use startsAt.
    loki_enrich_clamp_future_starts_at: bool
    # When clamp is true and startsAt is ahead of now but within this gap (minutes), query spans
    # [min(now,startsAt)-window .. max(now,startsAt)+window] so incident-time logs are not missed.
    # If gap exceeds this, anchor asymmetric window on startsAt (see loki.alert_time_window).
    loki_enrich_future_gap_span_max_minutes: int
    # After Grafana startsAt: cap query_range end at min(now, startsAt + this many minutes) so
    # backward queries do not flood samples with noisy logs between incident time and wall clock.
    loki_enrich_after_start_minutes: int
    # If startsAt-based query returns no streams but startsAt is ahead of agent clock (manual test),
    # retry once with ±window around agent now so Loki still returns recent logs when they exist.
    loki_enrich_empty_fallback_to_now: bool
    # If tight startsAt window returns no streams, retry the wide min(now)↔startsAt span (see README).
    loki_enrich_retry_wide_span_on_empty: bool
    # After query_range, reorder sampled lines by proximity to Grafana startsAt (Loki index time).
    loki_enrich_prioritize_starts_at: bool
    # Split line cap: percent reserved for log lines with index time *before* startsAt (lead-up).
    loki_enrich_stratify_lines: bool
    loki_enrich_line_budget_before_pct: int
    # Second query_range pass with |~ regex (merged with broad results before line-cap normalize).
    loki_enrich_error_pass_enabled: bool
    loki_enrich_error_logql_regex: str
    loki_query_timeout_seconds: float
    # backward = newest first (default); forward = oldest first in window (better lead-up / INFO before ERROR).
    loki_query_direction: str
    loki_query_limit: int
    loki_result_max_streams: int
    loki_result_max_total_lines: int
    slack_webhook_url: str
    slack_notify_enabled: bool
    slack_timeout_seconds: float
    # Slack Events API (thread replies → RCA rerun); verify signatures with signing secret.
    slack_signing_secret: str
    slack_bot_token: str
    # After POST /events/{id}/rca/rerun (or Slack thread command): post full Incoming Webhook digest when True.
    notify_slack_on_rca_rerun: bool
    # Replace generic Loki/label operator bullets with one targeted line when delivery is low-signal
    # (e.g. DatasourceNoData, insufficient labels, all Loki attempts failed).
    slack_smart_skip_operator_checklist: bool
    # Ollama (same host as ai-model): RCA text for Slack / stored enrichment.
    ollama_host: str
    ollama_model: str
    ollama_rca_enabled: bool
    # Passed to Ollama /api/chat as options.{temperature,top_p}; None = omit (server default).
    ollama_rca_temperature: Optional[float]
    ollama_rca_top_p: Optional[float]
    ollama_timeout_seconds: float
    ollama_rca_max_log_chars: int
    # When True and logs match simulate-error / simulated demo signals, emit fixed RCA text (skip LLM).
    ollama_rca_simulated_template_override: bool
    # Max chars for optional operator instructions on RCA rerun (Slack / POST body).
    ollama_rca_user_prompt_max_chars: int
    ollama_health_timeout_seconds: float
    ollama_health_require_model: bool


def _loki_query_direction() -> str:
    raw = os.getenv("LOKI_QUERY_DIRECTION", "backward").strip().lower()
    if raw in ("backward", "forward"):
        return raw
    return "backward"


def _loki_anchor() -> str:
    raw = os.getenv("LOKI_ENRICH_ANCHOR", "alert_start").strip().lower()
    if raw in ("alert_start", "now"):
        return raw
    return "alert_start"


def _optional_float_env(name: str) -> Optional[float]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid float for %s — ignoring", name)
        return None


def _enrich_line_budget_before_pct() -> int:
    try:
        v = int(os.getenv("LOKI_ENRICH_LINE_BUDGET_BEFORE_PCT", "50"))
    except ValueError:
        return 50
    return max(0, min(100, v))


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int, *, min_value: Optional[int] = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r; using default %s", name, raw, default)
        return default
    if min_value is not None and v < min_value:
        logger.warning("%s=%s below min %s; using min", name, v, min_value)
        return min_value
    return v


def _load_settings() -> Settings:
    return Settings(
        webhook_secret=os.getenv("WEBHOOK_SECRET", "").strip(),
        database_url=os.getenv("DATABASE_URL", "").strip(),
        postgres_host=os.getenv("POSTGRES_HOST", "postgres").strip(),
        postgres_port=_int_env("POSTGRES_PORT", 5432, min_value=1),
        postgres_user=os.getenv("POSTGRES_USER", "sre").strip(),
        postgres_password=os.getenv("POSTGRES_PASSWORD", "").strip(),
        postgres_db=os.getenv("POSTGRES_DB", "sre_agent").strip(),
        db_connect_timeout_seconds=_int_env("DB_CONNECT_TIMEOUT_SECONDS", 5, min_value=1),
        db_pool_max_size=_int_env("DB_POOL_MAX_SIZE", 10, min_value=1),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        loki_url_prod=os.getenv("LOKI_URL_PROD", "").strip(),
        loki_url_nonprod=os.getenv("LOKI_URL_NONPROD", "").strip(),
        loki_url_default=os.getenv("LOKI_URL_DEFAULT", "").strip(),
        loki_bearer_token=os.getenv("LOKI_BEARER_TOKEN", "").strip(),
        loki_scope_org_id=os.getenv("LOKI_SCOPE_ORG_ID", "").strip(),
        loki_enrich_enabled=_bool_env("LOKI_ENRICH_ENABLED", True),
        loki_enrich_window_minutes=_int_env("LOKI_ENRICH_WINDOW_MINUTES", 10, min_value=1),
        loki_enrich_anchor=_loki_anchor(),
        loki_enrich_clamp_future_starts_at=_bool_env("LOKI_ENRICH_CLAMP_FUTURE_STARTS_AT", True),
        loki_enrich_future_gap_span_max_minutes=_int_env(
            "LOKI_ENRICH_FUTURE_GAP_SPAN_MAX_MINUTES", 180, min_value=1
        ),
        loki_enrich_after_start_minutes=_int_env("LOKI_ENRICH_AFTER_START_MINUTES", 5, min_value=0),
        loki_enrich_empty_fallback_to_now=_bool_env("LOKI_ENRICH_EMPTY_FALLBACK_TO_NOW", True),
        loki_enrich_retry_wide_span_on_empty=_bool_env("LOKI_ENRICH_RETRY_WIDE_SPAN_ON_EMPTY", True),
        loki_enrich_prioritize_starts_at=_bool_env("LOKI_ENRICH_PRIORITIZE_STARTS_AT", True),
        loki_enrich_stratify_lines=_bool_env("LOKI_ENRICH_STRATIFY_LINES", True),
        loki_enrich_line_budget_before_pct=_enrich_line_budget_before_pct(),
        loki_enrich_error_pass_enabled=_bool_env("LOKI_ENRICH_ERROR_PASS_ENABLED", True),
        loki_enrich_error_logql_regex=os.getenv(
            "LOKI_ENRICH_ERROR_LOGQL_REGEX",
            "(?i)(error|exception|fatal|panic|warn)",
        ).strip(),
        loki_query_timeout_seconds=float(os.getenv("LOKI_QUERY_TIMEOUT_SECONDS", "15")),
        loki_query_direction=_loki_query_direction(),
        loki_query_limit=_int_env("LOKI_QUERY_LIMIT", 500, min_value=1),
        loki_result_max_streams=_int_env("LOKI_RESULT_MAX_STREAMS", 20, min_value=1),
        loki_result_max_total_lines=_int_env("LOKI_RESULT_MAX_TOTAL_LINES", 100, min_value=1),
        slack_webhook_url=_normalize_slack_incoming_webhook_url(os.getenv("SLACK_WEBHOOK_URL", "")),
        slack_notify_enabled=_bool_env("SLACK_NOTIFY_ENABLED", True),
        slack_timeout_seconds=float(os.getenv("SLACK_TIMEOUT_SECONDS", "10")),
        slack_signing_secret=os.getenv("SLACK_SIGNING_SECRET", "").strip(),
        slack_bot_token=os.getenv("SLACK_BOT_TOKEN", "").strip(),
        notify_slack_on_rca_rerun=_bool_env("NOTIFY_SLACK_ON_RCA_RERUN", False),
        slack_smart_skip_operator_checklist=_bool_env("SLACK_SMART_SKIP_OPERATOR_CHECKLIST", True),
        ollama_host=os.getenv("OLLAMA_HOST", "http://ollama:11434").strip(),
        ollama_model=os.getenv("OLLAMA_MODEL", "llama3.2").strip(),
        ollama_rca_enabled=_bool_env("OLLAMA_RCA_ENABLED", True),
        ollama_rca_temperature=_optional_float_env("OLLAMA_RCA_TEMPERATURE"),
        ollama_rca_top_p=_optional_float_env("OLLAMA_RCA_TOP_P"),
        ollama_timeout_seconds=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "120")),
        ollama_rca_max_log_chars=_int_env("OLLAMA_RCA_MAX_LOG_CHARS", 12000, min_value=1000),
        ollama_rca_simulated_template_override=_bool_env("OLLAMA_RCA_SIMULATED_TEMPLATE_OVERRIDE", True),
        ollama_rca_user_prompt_max_chars=_int_env(
            "OLLAMA_RCA_USER_PROMPT_MAX_CHARS", 2000, min_value=100
        ),
        ollama_health_timeout_seconds=float(os.getenv("OLLAMA_HEALTH_TIMEOUT_SECONDS", "5")),
        ollama_health_require_model=_bool_env("OLLAMA_HEALTH_REQUIRE_MODEL", True),
    )


settings = _load_settings()
