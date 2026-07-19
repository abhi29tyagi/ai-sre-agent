"""Tests for suppressing generic operator checklist on low-signal Loki/Grafana deliveries."""

from app.services.suggestions import (
    _selector_attempts_all_errors,
    should_skip_operator_checklist,
)


def test_skip_for_datasource_no_data_alertname() -> None:
    s, code = should_skip_operator_checklist(
        {"alertname": "DatasourceNoData"},
        {"skipped": True, "reason": "insufficient_labels_for_logql_selector"},
        enabled=True,
    )
    assert s is True
    assert code == "grafana_no_data_alert"


def test_skip_insufficient_labels_even_if_alertname_normal() -> None:
    s, code = should_skip_operator_checklist(
        {"alertname": "HighErrorRate"},
        {"skipped": True, "reason": "insufficient_labels_for_logql_selector"},
        enabled=True,
    )
    assert s is True
    assert code == "insufficient_labels_for_logql_selector"


def test_no_skip_for_unknown_skip_reason() -> None:
    s, code = should_skip_operator_checklist(
        {"alertname": "Weird"},
        {"skipped": True, "reason": "custom_internal_reason"},
        enabled=True,
    )
    assert s is False
    assert code == ""


def test_skip_when_all_loki_attempts_failed() -> None:
    enr = {
        "skipped": False,
        "selector_attempts": [
            {"pass": "primary", "query": "{x=~`y`}", "error": "http_error", "http_status": 502},
            {"pass": "primary", "query": "{x=~`z`}", "error": "ConnectTimeout"},
        ],
        "data": {"result": []},
    }
    assert _selector_attempts_all_errors(enr) is True
    s, code = should_skip_operator_checklist({"alertname": "AppDown"}, enr, enabled=True)
    assert s is True
    assert code == "loki_query_transport_failure"


def test_no_skip_when_loki_returned_structured_empty_stream() -> None:
    enr = {
        "skipped": False,
        "selector_attempts": [
            {"pass": "primary", "query": "{a=\"b\"}", "streams": 0, "lines": 0},
        ],
        "data": {"result": []},
    }
    assert _selector_attempts_all_errors(enr) is False


def test_enabled_false_forces_no_skip() -> None:
    s, _ = should_skip_operator_checklist(
        {"alertname": "DatasourceNoData"},
        None,
        enabled=False,
    )
    assert s is False
