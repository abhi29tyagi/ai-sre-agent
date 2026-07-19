"""TTL-based dedupe for Slack Events API deliveries.

Slack retries event delivery if your endpoint does not 200 within ~3 seconds, and the same
``event_id`` may also arrive via a stuck retry while the background RCA job is already running.
This in-memory store lets us drop duplicates within a short window without a DB round-trip.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

_DEFAULT_TTL_SECONDS = 300


class SlackEventDedupe:
    def __init__(self, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()
        self._seen: dict[str, float] = {}

    async def claim(self, event_id: Optional[str]) -> bool:
        """
        Return True if this is the first time we've seen ``event_id`` recently
        (caller should process the event). Return False to skip a duplicate.

        Empty / missing ``event_id`` is always treated as fresh.
        """
        if not event_id:
            return True
        async with self._lock:
            now = time.time()
            self._purge(now)
            if event_id in self._seen:
                return False
            self._seen[event_id] = now
            return True

    def _purge(self, now: float) -> None:
        cutoff = now - self._ttl
        expired = [k for k, t in self._seen.items() if t < cutoff]
        for k in expired:
            self._seen.pop(k, None)


slack_event_dedupe = SlackEventDedupe()
