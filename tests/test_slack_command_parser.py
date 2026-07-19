"""Tests for the rca/rerun command parser used by Slack thread replies."""

from app.routes.slack_events import _parse_rca_command


def test_basic_rca_and_rerun_synonyms() -> None:
    assert _parse_rca_command("rca 19") == (19, None, None)
    assert _parse_rca_command("rerun 19") == (19, None, None)
    assert _parse_rca_command("RCA event 19") == (19, None, None)
    assert _parse_rca_command("rerun #19") == (19, None, None)


def test_strips_user_mention() -> None:
    assert _parse_rca_command("<@U12345> rerun 7") == (7, None, None)


def test_model_override() -> None:
    assert _parse_rca_command("rca 19 llama3.2") == (19, "llama3.2", None)
    assert _parse_rca_command("rerun 19 mistral") == (19, "mistral", None)


def test_dash_dash_prompt_only() -> None:
    assert _parse_rca_command("rerun 19 -- focus on mongo") == (19, None, "focus on mongo")


def test_model_and_dash_dash_prompt() -> None:
    assert _parse_rca_command("rerun 19 llama3.2 -- narrow to customer impact") == (
        19,
        "llama3.2",
        "narrow to customer impact",
    )


def test_implicit_prompt_without_dashes() -> None:
    out = _parse_rca_command("rerun 19 please assess blast radius")
    assert out == (19, None, "please assess blast radius")


def test_invalid_inputs() -> None:
    assert _parse_rca_command("hello world") is None
    assert _parse_rca_command("") is None
    assert _parse_rca_command("rca abc") is None
