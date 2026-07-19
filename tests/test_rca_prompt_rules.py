"""Smoke checks that critical RCA prompt rules stay in the user prompt.

These rules are the model regressions we hit most: the tests are not
comprehensive, just a tripwire so a future refactor does not silently drop them.
"""

from __future__ import annotations

from typing import Optional

from app.services.ollama_rca import _build_user_prompt


def _prompt(extra: Optional[str] = None) -> str:
    return _build_user_prompt(
        summary={"alertname": "Test", "status": "firing"},
        log_blob="(no logs)",
        operator_instructions=extra,
    )


def test_prompt_forbids_path_200_then_401_mix() -> None:
    p = _prompt()
    assert "statusCode 200" in p
    assert "false RCA" in p


def test_prompt_blocks_route_attribution_without_path() -> None:
    p = _prompt()
    assert "path: or url:" in p
    assert "failing route not shown next to the stack" in p


def test_prompt_keeps_annotation_rule_in_likely_cause_and_next_steps() -> None:
    p = _prompt()
    assert "annotation_summary" in p
    assert "*Next steps*" in p
    # next-steps protection — operator should not be sent to fix annotations
    assert "On-call acts on services and logs, not annotations." in p


def test_prompt_blocks_single_correlationid_repeats_inference() -> None:
    p = _prompt()
    assert "Incoming request" in p
    assert "DISTINCT correlationIds" in p


def test_prompt_requires_single_asterisk_titles() -> None:
    p = _prompt()
    assert "EXACTLY ONE asterisk" in p
    assert "***bold-italic***" in p


def test_prompt_forbids_meta_preamble_and_numbered_lists() -> None:
    p = _prompt()
    assert "FIRST visible output text must be *Likely cause*" in p
    assert "forbid numbered lists" in p
    assert "unclear if database or external service outage" in p


def test_prompt_simulated_route_vendor_escalation_in_user_prompt() -> None:
    p = _build_user_prompt(
        summary={"alertname": "X"},
        log_blob='ERROR url":"/api/simulate-error" "simulated":true',
    )
    assert "/api/simulate-error" in p
    assert "positive" in p.lower() and "operator actions" in p


def test_prompt_bans_annotation_summary_everywhere() -> None:
    p = _prompt()
    assert "annotation_summary" in p
    assert "anywhere in the RCA" in p or "any section" in p


def test_prompt_never_invent_correlation_id() -> None:
    p = _prompt()
    assert "req.id" in p
    assert "Never invent" in p


def test_prompt_slack_readability_block_present() -> None:
    p = _prompt()
    assert "Slack readability" in p
    assert "classification checklists" in p


def test_prompt_same_request_attribution_block() -> None:
    p = _prompt()
    assert "(A2) Same-request attribution" in p
    assert "manual stderr sample" in p
    assert "same" in p.lower() and "req.id" in p


def test_prompt_simulated_next_steps_avoids_vague_investigate() -> None:
    p = _build_user_prompt(
        summary={"alertname": "X"},
        log_blob='ERROR "/api/simulate-error" "simulated":true',
    )
    assert "investigate why the route is accessed" in p
    assert "production-ready fix" in p


def test_prompt_likely_cause_prose_only_simulated() -> None:
    p = _build_user_prompt(
        summary={"alertname": "X"},
        log_blob='simulated error path "/api/simulate-error"',
    )
    assert "two sentences of prose only" in p
    assert "*Likely cause* is **never** a bullet list" in p


def test_prompt_additional_context_no_placeholder() -> None:
    p = _prompt()
    assert "no additional issues" in p.lower()
    assert "**omit** *Additional context* completely" in p


def test_prompt_evidence_no_fake_shared_req_id() -> None:
    p = _prompt()
    assert "shared between WARN and ERROR" in p
    assert "Incoming request" in p and "same line" in p


def test_prompt_simulated_sentence_one_err_message_first() -> None:
    p = _build_user_prompt(
        summary={"alertname": "X"},
        log_blob='simulated error path "/api/simulate-error"',
    )
    assert "begin with (or immediately quote)" in p


def test_prompt_bans_nested_evidence_bullets_and_db_negation() -> None:
    p = _prompt()
    assert "nest extra" in p
    assert "No upstream database mentioned" in p


def test_prompt_bans_production_ready_endpoints_phrasing() -> None:
    p = _build_user_prompt(
        summary={"alertname": "X"},
        log_blob='"/api/simulate-error" simulated',
    )
    assert "production-ready endpoints" in p


def test_prompt_simulated_bans_investigate_endpoint_failure_and_grafana_annotations() -> None:
    p = _build_user_prompt(
        summary={"alertname": "X"},
        log_blob='ERROR "/api/simulate-error" simulated error path',
    )
    assert "understand why it's failing" in p or "why it's failing" in p
    assert "annotation_summary" in p
    assert "test enrichment" in p.lower()


def test_prompt_evidence_bans_placeholder_ids_and_field_colons() -> None:
    p = _prompt()
    assert "`XXXXXXXX`" in p
    assert "chained pseudo-fields" in p


def test_prompt_appends_final_reminder_when_simulated_signals_in_blob() -> None:
    p = _build_user_prompt(
        summary={"alertname": "X"},
        log_blob='ERROR GET url":"/api/simulate-error" "simulated":true',
    )
    assert "FINAL REMINDER" in p
    assert "Identify the specific route" in p
    assert "downstream issue" in p


def test_prompt_no_final_reminder_without_simulated_signals() -> None:
    p = _build_user_prompt(
        summary={"alertname": "X"},
        log_blob='WARN validation_failed url":"/api/orders"',
    )
    assert "FINAL REMINDER" not in p


def test_prompt_includes_operator_instructions_when_supplied() -> None:
    p = _prompt(extra="Focus on Mongo timeouts")
    assert "Operator-supplied notes" in p
    assert "Focus on Mongo timeouts" in p


def test_prompt_no_error_signals_branch_emits_directive() -> None:
    """Excerpt with only socket / GET / params lines must not get an auth-failure RCA."""
    from app.services.ollama_rca import _build_user_prompt

    benign = (
        "socket.handshake.query =>  { EIO: '4', transport: 'websocket' }\n"
        "user connect => OLf5rTvwjdycRKxUAADr\n"
        "Method ->  GET  =>  https://api.example.com/v1/health\n"
        "params ->  {}\n"
        "query ->  {}\n"
    )
    p = _build_user_prompt(summary={"alertname": "X"}, log_blob=benign)
    assert "error_signals_present: NO" in p
    assert "No error-level lines visible" in p


def test_prompt_error_signals_branch_when_warn_present() -> None:
    from app.services.ollama_rca import _build_user_prompt

    noisy = "WARN [HttpExceptionFilter] Object(9) { message: 'Exception caught' }"
    p = _build_user_prompt(summary={"alertname": "X"}, log_blob=noisy)
    assert "error_signals_present: yes" in p
    assert "No error-level lines visible" not in p


def test_prompt_when_errors_present_forbids_nest_authguard_for_express_stack() -> None:
    """Regression: Express/pino sample-logging-app logs must not produce Nest/AuthGuard RCA."""
    p = _build_user_prompt(
        summary={"alertname": "X"},
        log_blob='ERROR Simulated downstream failure "simulated":true express/lib/router',
    )
    assert "error_signals_present: yes" in p
    assert "CRITICAL — error_signals_present is YES" in p
    assert "pino-http" in p
    assert "simulated\": true" in p or '"simulated": true' in p


def test_prompt_additional_context_cannot_invent_auth_db_thread() -> None:
    p = _prompt()
    assert "Additional context" in p
    assert "simulate-error" in p or "simulated" in p.lower()


def test_prompt_blocks_naming_specific_exceptions_without_evidence() -> None:
    from app.services.ollama_rca import _build_user_prompt

    p = _build_user_prompt(summary={"alertname": "X"}, log_blob="(no logs)")
    assert "Do NOT name specific exception classes" in p
    assert "AuthGuard" in p
