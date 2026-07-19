"""Verify Slack request signatures (Events API, Interactivity)."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Final


_SKEW_SECONDS: Final[int] = 60 * 5


def verify_slack_signature(*, signing_secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    """
    Verify ``X-Slack-Signature`` for raw POST body (Events API ``event_callback``).

    Reject requests older than five minutes (replay protection).
    """
    if not signing_secret or not timestamp or not signature or not body:
        return False
    try:
        ts_int = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - ts_int) > _SKEW_SECONDS:
        return False
    sig_basestring = b"v0:" + timestamp.encode("ascii") + b":" + body
    my_sig = (
        "v0="
        + hmac.new(signing_secret.encode("utf-8"), sig_basestring, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(my_sig, signature)
