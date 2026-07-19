"""Tests for Slack request signature verification."""

from __future__ import annotations

import hashlib
import hmac
import time

from app.services.slack_verify import verify_slack_signature


def _sign(secret: str, body: bytes, ts: str) -> str:
    base = b"v0:" + ts.encode("ascii") + b":" + body
    return "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()


def test_accepts_valid_signature() -> None:
    secret = "abcdef"
    body = b'{"hello":"world"}'
    ts = str(int(time.time()))
    sig = _sign(secret, body, ts)
    assert verify_slack_signature(signing_secret=secret, timestamp=ts, body=body, signature=sig)


def test_rejects_bad_signature() -> None:
    secret = "abcdef"
    body = b'{"hello":"world"}'
    ts = str(int(time.time()))
    assert not verify_slack_signature(
        signing_secret=secret, timestamp=ts, body=body, signature="v0=deadbeef"
    )


def test_rejects_old_timestamp() -> None:
    secret = "abcdef"
    body = b'{"hello":"world"}'
    ts = str(int(time.time()) - 10_000)
    sig = _sign(secret, body, ts)
    assert not verify_slack_signature(signing_secret=secret, timestamp=ts, body=body, signature=sig)


def test_rejects_missing_pieces() -> None:
    assert not verify_slack_signature(signing_secret="", timestamp="123", body=b"a", signature="v0=x")
    assert not verify_slack_signature(signing_secret="abc", timestamp="", body=b"a", signature="v0=x")
    assert not verify_slack_signature(signing_secret="abc", timestamp="not-a-number", body=b"a", signature="v0=x")
