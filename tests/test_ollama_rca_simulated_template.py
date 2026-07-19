"""Deterministic RCA for sample-logging-app ``/api/simulate-error`` when template override is on."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

import app.services.ollama_rca as ollama_rca_mod
from app.services.ollama_rca import (
    _extract_simulated_err_message,
    _render_simulated_demo_rca,
    generate_rca_ollama,
)


_SAMPLE_LINE = (
    '{"level":"ERROR","req":{"method":"GET","url":"/api/simulate-error","id":547},'
    '"err":{"type":"Error","message":"Simulated downstream failure (payment gateway timeout)"},'
    '"msg":"simulated error path"}'
)


def test_extract_simulated_err_message_from_json_line() -> None:
    assert _extract_simulated_err_message(_SAMPLE_LINE) == (
        "Simulated downstream failure (payment gateway timeout)"
    )


def test_render_simulated_demo_rca_has_three_sections() -> None:
    text = _render_simulated_demo_rca("Simulated downstream failure (demo).")
    assert "*Likely cause*" in text
    assert "*Evidence*" in text
    assert "*Next steps*" in text
    assert "/api/simulate-error" in text
    assert "XXXXXXXX" not in text


def test_generate_rca_uses_template_without_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ollama_rca_mod,
        "settings",
        replace(
            ollama_rca_mod.settings,
            ollama_rca_simulated_template_override=True,
            ollama_rca_enabled=True,
        ),
    )

    enrichment = {
        "data": {
            "result": [
                {"values": [["1735123456789000000", _SAMPLE_LINE]]},
            ]
        }
    }

    async def _run() -> None:
        out = await generate_rca_ollama({}, {"alertname": "Test"}, enrichment)
        assert out.get("simulated_demo_template") is True
        assert out.get("text")
        assert "Simulated downstream failure (payment gateway timeout)" in out["text"]
        assert "Identify the specific route" not in out["text"]

    asyncio.run(_run())


def test_generate_rca_skips_template_when_operator_prompt_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ollama_rca_mod,
        "settings",
        replace(
            ollama_rca_mod.settings,
            ollama_rca_simulated_template_override=True,
            ollama_rca_enabled=True,
            ollama_host="http://127.0.0.1:11434",
        ),
    )
    monkeypatch.setattr(
        "app.services.ollama_rca.assert_ollama_ready_for_rca",
        AsyncMock(return_value=None),
    )

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"message": {"content": "*Likely cause*\n\nstub from model"}}

    class _Client:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        async def post(self, *_a: object, **_k: object) -> _Resp:
            return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _Client)

    enrichment = {
        "data": {
            "result": [
                {"values": [["1735123456789000000", _SAMPLE_LINE]]},
            ]
        }
    }

    async def _run() -> None:
        out = await generate_rca_ollama(
            {},
            {"alertname": "Test"},
            enrichment,
            user_prompt="focus on latency",
        )
        assert out.get("simulated_demo_template") is not True
        assert out.get("text") == "*Likely cause*\n\nstub from model"

    asyncio.run(_run())


def test_extract_returns_none_when_no_message_field() -> None:
    blob = '{"level":"ERROR","req":{"url":"/api/simulate-error"}}'
    assert _extract_simulated_err_message(blob) is None
