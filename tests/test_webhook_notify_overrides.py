"""Tests for POST /hooks/grafana per-request Slack / Ollama overrides."""

from starlette.datastructures import QueryParams

from app.services.webhook_notify_overrides import (
    parse_optional_bool,
    resolve_grafana_webhook_notify_flags,
)


def test_parse_optional_bool() -> None:
    assert parse_optional_bool(None) is None
    assert parse_optional_bool("") is None
    assert parse_optional_bool("  ") is None
    assert parse_optional_bool("maybe") is None
    assert parse_optional_bool("TRUE") is True
    assert parse_optional_bool("0") is False
    assert parse_optional_bool("no") is False


def test_query_params_override_settings() -> None:
    payload = {"alerts": [{"labels": {"alertname": "x"}, "annotations": {}}]}
    qp = QueryParams("slack_notify=false&ollama_rca=false")
    s, o = resolve_grafana_webhook_notify_flags(
        payload,
        qp,
        settings_slack_notify=True,
        settings_ollama_rca=True,
    )
    assert s is False
    assert o is False


def test_annotation_overrides_query() -> None:
    payload = {
        "alerts": [
            {
                "labels": {},
                "annotations": {"ai_sre_notify_slack": "true", "ai_sre_ollama_rca": "false"},
            }
        ]
    }
    qp = QueryParams("slack_notify=false&ollama_rca=true")
    s, o = resolve_grafana_webhook_notify_flags(
        payload,
        qp,
        settings_slack_notify=False,
        settings_ollama_rca=False,
    )
    assert s is True
    assert o is False


def test_label_used_when_annotation_absent() -> None:
    payload = {
        "alerts": [
            {
                "labels": {"ai_sre_notify_slack": "false"},
                "annotations": {},
            }
        ]
    }
    qp = QueryParams("")
    s, _ = resolve_grafana_webhook_notify_flags(
        payload,
        qp,
        settings_slack_notify=True,
        settings_ollama_rca=True,
    )
    assert s is False


def test_annotation_beats_label_for_same_key() -> None:
    payload = {
        "alerts": [
            {
                "labels": {"ai_sre_notify_slack": "true"},
                "annotations": {"ai_sre_notify_slack": "false"},
            }
        ]
    }
    qp = QueryParams("")
    s, _ = resolve_grafana_webhook_notify_flags(
        payload,
        qp,
        settings_slack_notify=True,
        settings_ollama_rca=True,
    )
    assert s is False
