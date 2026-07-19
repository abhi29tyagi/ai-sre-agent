"""Operator checklist must not claim HTTP 429 from incidental '429' substrings (timestamps, etc.)."""

from app.services.suggestions import _HTTP_429_RESPONSE_RE, build_operator_suggestions


def test_statuscode_429_matches() -> None:
    assert _HTTP_429_RESPONSE_RE.search('{"res":{"statusCode":429}}')
    assert _HTTP_429_RESPONSE_RE.search('"statusCode": 429')


def test_iso_timestamp_does_not_false_positive_429() -> None:
    """Plain substring '429' in blob used to fire on unrelated JSON — tighten regex."""
    blob = '{"time":"2026-05-05T11:29:00.257Z","level":"INFO","msg":"ok"}'
    assert not _HTTP_429_RESPONSE_RE.search(blob)


def test_build_operator_suggestions_no_429_hint_without_status() -> None:
    enrichment = {
        "skipped": False,
        "data": {
            "result": [
                {
                    "values": [
                        [
                            "1",
                            '{"time":"2026-05-05T11:29:00.257Z","level":"INFO"}',
                        ]
                    ]
                }
            ]
        },
        "query": '{service_name="x"}',
    }
    sug = build_operator_suggestions({"alertname": "t"}, enrichment)
    txt = " ".join(sug)
    assert "HTTP 429" not in txt
