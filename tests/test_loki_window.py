"""Tests for Loki time-window math (skew handling around Grafana startsAt)."""

from datetime import datetime, timedelta, timezone

from app.services.loki import _window_around_starts_at, _window_span_skew_bridging


def test_now_after_starts_at_caps_end_at_starts_plus_after() -> None:
    starts = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
    now = starts + timedelta(minutes=30)
    s, e = _window_around_starts_at(starts, now)
    assert s == starts - timedelta(minutes=10)  # default LOKI_ENRICH_WINDOW_MINUTES
    assert e == starts + timedelta(minutes=5)  # default LOKI_ENRICH_AFTER_START_MINUTES (now > cap)


def test_now_close_to_starts_caps_end_at_now() -> None:
    starts = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
    now = starts + timedelta(minutes=2)
    _, e = _window_around_starts_at(starts, now)
    assert e == now


def test_now_before_starts_uses_starts_plus_after() -> None:
    starts = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
    now = starts - timedelta(minutes=30)
    s, e = _window_around_starts_at(starts, now)
    assert s == starts - timedelta(minutes=10)
    assert e == starts + timedelta(minutes=5)


def test_end_never_collapses_below_start() -> None:
    starts = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
    now = starts - timedelta(hours=2)
    s, e = _window_around_starts_at(starts, now)
    assert e > s


def test_skew_bridging_when_agent_clock_behind_grafana_includes_starts_at() -> None:
    """Empty-primary fallback must span both clocks — symmetric-around-now alone misses the alert."""
    starts_at = datetime(2026, 4, 30, 15, 20, 0, tzinfo=timezone.utc)
    agent_now = datetime(2026, 4, 30, 11, 50, 0, tzinfo=timezone.utc)
    delta = timedelta(minutes=10)
    after = timedelta(minutes=5)
    earliest, latest = _window_span_skew_bridging(starts_at, agent_now, delta, after)
    assert earliest <= agent_now
    assert latest >= starts_at
    assert earliest == agent_now - delta
    assert latest == min(starts_at + delta, starts_at + after)
