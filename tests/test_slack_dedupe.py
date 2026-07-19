"""Tests for the Slack Events API in-memory dedupe."""

from __future__ import annotations

import asyncio

import pytest

from app.services.slack_dedupe import SlackEventDedupe


@pytest.mark.asyncio
async def test_first_claim_succeeds_then_blocks_repeat() -> None:
    d = SlackEventDedupe(ttl_seconds=10)
    assert await d.claim("Ev01") is True
    assert await d.claim("Ev01") is False


@pytest.mark.asyncio
async def test_distinct_ids_independent() -> None:
    d = SlackEventDedupe(ttl_seconds=10)
    assert await d.claim("Ev01") is True
    assert await d.claim("Ev02") is True


@pytest.mark.asyncio
async def test_empty_id_always_fresh() -> None:
    d = SlackEventDedupe(ttl_seconds=10)
    assert await d.claim(None) is True
    assert await d.claim("") is True


@pytest.mark.asyncio
async def test_purge_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    d = SlackEventDedupe(ttl_seconds=1)
    assert await d.claim("Ev01") is True

    import app.services.slack_dedupe as mod

    real_time = mod.time.time()
    monkeypatch.setattr(mod.time, "time", lambda: real_time + 10)
    assert await d.claim("Ev01") is True


def test_pytest_asyncio_default_event_loop() -> None:
    asyncio.new_event_loop().close()
