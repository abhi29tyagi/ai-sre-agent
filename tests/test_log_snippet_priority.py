"""Slack log snippet ordering — ERROR/WARN lines should beat repetitive INFO /health lines."""

from app.services.suggestions import prioritize_log_lines_for_snippets


def test_prioritize_moves_json_error_before_info_health() -> None:
    info_health = '{"level":"INFO","req":{"url":"/health"}}'
    info_other = '{"level":"INFO","req":{"url":"/api/hello"}}'
    err_sim = '{"level":"ERROR","msg":"simulated error path"}'
    raw = [info_health, info_other, info_health, err_sim, info_health]
    out = prioritize_log_lines_for_snippets(raw)
    assert out[0] == err_sim


def test_prioritize_keeps_order_within_hot_and_cold() -> None:
    e1 = '{"level":"ERROR","n":1}'
    e2 = '{"level":"WARN","n":2}'
    i1 = '{"level":"INFO"}'
    raw = [i1, e1, i1, e2]
    out = prioritize_log_lines_for_snippets(raw)
    assert out[:2] == [e1, e2]
    assert i1 in out[2:]
