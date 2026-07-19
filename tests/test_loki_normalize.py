"""Tests for Loki result normalization (line cap + anchor-based stratify)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from app.services.loki import _normalize_loki_result


def _streams_dict(streams: list[Dict[str, Any]]) -> Dict[str, Any]:
    return {"resultType": "streams", "result": streams}


def test_no_anchor_keeps_lines_in_order() -> None:
    data = _streams_dict(
        [
            {"stream": {"a": "1"}, "values": [["1", "line A"], ["2", "line B"]]},
        ]
    )
    out = _normalize_loki_result(data)
    assert out["result"] == [
        {"stream": {"a": "1"}, "values": [["1", "line A"], ["2", "line B"]]}
    ]
    assert out["lines_truncated"] is False
    assert out["streams_truncated"] is False


def test_anchor_orders_streams_chronologically() -> None:
    anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
    anchor_ns = int(anchor.timestamp() * 1_000_000_000)
    data = _streams_dict(
        [
            {
                "stream": {"a": "1"},
                "values": [
                    [str(anchor_ns - 1000), "near-before"],
                    [str(anchor_ns + 1000), "near-after"],
                    [str(anchor_ns + 60_000_000_000), "far-after"],
                ],
            }
        ]
    )
    out = _normalize_loki_result(data, anchor_dt=anchor)
    streams = out["result"]
    assert len(streams) == 1
    assert [v[1] for v in streams[0]["values"]] == ["near-before", "near-after", "far-after"]


def test_anchor_stratify_keeps_before_and_after() -> None:
    """With stratify enabled, the cap is split between lines before and after the anchor."""
    anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
    anchor_ns = int(anchor.timestamp() * 1_000_000_000)

    before_values = [[str(anchor_ns - i), f"before-{i}"] for i in range(1, 81)]
    after_values = [[str(anchor_ns + i), f"after-{i}"] for i in range(1, 81)]
    data = _streams_dict(
        [{"stream": {"a": "1"}, "values": before_values + after_values}]
    )
    out = _normalize_loki_result(data, anchor_dt=anchor)
    kept = [v[1] for v in out["result"][0]["values"]]
    assert any(name.startswith("before-") for name in kept)
    assert any(name.startswith("after-") for name in kept)
    assert out["lines_truncated"] is True
