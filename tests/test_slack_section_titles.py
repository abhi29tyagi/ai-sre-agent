"""Tests for collapsing ``**Title**`` / ``***Title***`` to single-asterisk Slack bold."""

from app.services.slack_notify import _spacing_for_slack_mrkdwn
from app.services.suggestions import (
    _collapse_section_title_asterisks,
    _ensure_blank_line_before_rca_sections,
    redact_for_slack,
)


def test_triple_asterisks_collapse_to_single() -> None:
    out = _collapse_section_title_asterisks("***Likely cause*** body text")
    assert out.startswith("*Likely cause*\n\n")
    assert "body text" in out
    assert "***" not in out


def test_double_asterisks_collapse() -> None:
    out = _collapse_section_title_asterisks("**Evidence** - bullet")
    assert "*Evidence*\n\n- bullet" in out


def test_single_asterisks_kept() -> None:
    out = _collapse_section_title_asterisks("*Next steps*\n\n- one")
    # No change other than whitespace normalization
    assert out.startswith("*Next steps*\n\n- one")


def test_multiple_titles_in_one_blob() -> None:
    raw = "***Likely cause*** A. **Evidence** - x. ***Next steps*** - do y."
    out = _collapse_section_title_asterisks(raw)
    assert out.count("*Likely cause*") == 1
    assert out.count("*Evidence*") == 1
    assert out.count("*Next steps*") == 1
    assert "***" not in out
    assert "**" not in out.replace("*Likely cause*", "").replace("*Evidence*", "").replace("*Next steps*", "")


def test_additional_context_handled() -> None:
    out = _collapse_section_title_asterisks("***Additional context*** unrelated")
    assert "*Additional context*\n\n" in out


def test_strips_leading_rca_preamble() -> None:
    out = _collapse_section_title_asterisks("**RCA**\n\n*Likely cause*\n\nbody")
    assert not out.startswith("**RCA")
    assert out.startswith("*Likely cause*")


def test_strips_root_cause_analysis_preamble() -> None:
    out = _collapse_section_title_asterisks("***Root Cause Analysis***\n\n*Likely cause* body")
    assert not out.lower().startswith("**root")
    assert out.startswith("*Likely cause*")


def test_glued_evidence_gets_blank_line_before_title() -> None:
    raw = (
        "No error-level lines visible in the Loki window queried for this alert. *Evidence* "
        "- The excerpt contains request paths."
    )
    out = _ensure_blank_line_before_rca_sections(raw)
    assert "alert.\n\n*Evidence*" in out


def test_glued_next_steps_gets_blank_line_before_title() -> None:
    raw = (
        "which could indicate noise without customer impact. *Next steps* - Verify that the startAt"
    )
    out = _ensure_blank_line_before_rca_sections(raw)
    assert "impact.\n\n*Next steps*" in out


def test_full_pipeline_after_redact_collapses_whitespace() -> None:
    """``redact_for_slack`` collapses newlines; spacing fix still separates glued section titles."""
    rca = """*Likely cause*

No error-level lines visible. *Evidence*

- bullet one.

- bullet two. *Next steps*

- verify clocks."""
    shaped = _spacing_for_slack_mrkdwn(redact_for_slack(rca.strip(), max_len=7500))
    assert ".\n\n*Evidence*" in shaped
    assert ".\n\n*Next steps*" in shaped
