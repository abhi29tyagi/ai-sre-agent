"""Test bootstrap: set env defaults so app.config.settings loads cleanly at import time."""

from __future__ import annotations

import os

os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("POSTGRES_PASSWORD", "test-password")
os.environ.setdefault("LOKI_ENRICH_WINDOW_MINUTES", "10")
os.environ.setdefault("LOKI_ENRICH_AFTER_START_MINUTES", "5")
os.environ.setdefault("LOKI_RESULT_MAX_TOTAL_LINES", "100")
os.environ.setdefault("LOKI_RESULT_MAX_STREAMS", "20")
os.environ.setdefault("LOKI_ENRICH_LINE_BUDGET_BEFORE_PCT", "50")
os.environ.setdefault("LOKI_ENRICH_STRATIFY_LINES", "true")
os.environ.setdefault("LOKI_ENRICH_PRIORITIZE_STARTS_AT", "true")
