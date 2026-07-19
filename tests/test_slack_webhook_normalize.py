"""Tests for Slack Incoming Webhook URL normalization."""

from app.config import _normalize_slack_incoming_webhook_url


def test_keeps_valid_url() -> None:
    url = "https://hooks.slack.com/services/T123/B456/abcdef"
    assert _normalize_slack_incoming_webhook_url(url) == url


def test_takes_last_when_nested() -> None:
    nested = (
        "https://hooks.slack.com/services/T1/B2/aaa"
        "https://hooks.slack.com/services/T9/B8/zzz"
    )
    assert _normalize_slack_incoming_webhook_url(nested) == (
        "https://hooks.slack.com/services/T9/B8/zzz"
    )


def test_blank_returns_blank() -> None:
    assert _normalize_slack_incoming_webhook_url("") == ""
    assert _normalize_slack_incoming_webhook_url("   ") == ""


def test_unrecognized_input_returned_as_is() -> None:
    raw = "not-a-url"
    assert _normalize_slack_incoming_webhook_url(raw) == raw
