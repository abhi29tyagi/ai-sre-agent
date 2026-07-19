"""Tests for the error-focused selector helper.

The error pass should append the LogQL regex pipeline to selectors it is given. We rely on
``enrich_grafana_alert`` passing only the broad-pass winner so we don't fan out to N selectors.
"""

from app.services.loki import error_focused_selector_variants


def test_one_selector_in_yields_one_query() -> None:
    out = error_focused_selector_variants(
        ['{service_name="/pulseworld-backend"}'],
        "(?i)(error|exception|warn)",
    )
    assert out == [
        '{service_name="/pulseworld-backend"} |~ "(?i)(error|exception|warn)"'
    ]


def test_dedupes_repeated_input_selectors() -> None:
    out = error_focused_selector_variants(
        ['{service_name="X"}', '{service_name="X"}'],
        "error",
    )
    assert out == ['{service_name="X"} |~ "error"']


def test_empty_regex_returns_no_queries() -> None:
    out = error_focused_selector_variants(
        ['{service_name="X"}'],
        "   ",
    )
    assert out == []


def test_escapes_backslash_and_quote_in_regex() -> None:
    out = error_focused_selector_variants(
        ['{service_name="X"}'],
        '\\d+ "boom"',
    )
    assert out == ['{service_name="X"} |~ "\\\\d+ \\"boom\\""']
