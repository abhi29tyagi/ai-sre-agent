"""Ollama readiness via GET /api/tags (daemon up + configured model present)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def _normalize_model_base(name: str) -> str:
    return name.split(":", 1)[0].strip().lower()


def _model_is_available(tags_payload: Dict[str, Any], want: str) -> bool:
    """Match OLLAMA_MODEL to entries like `llama3.2:latest` or exact name."""
    want_clean = want.strip()
    if not want_clean:
        return False
    want_base = _normalize_model_base(want_clean)
    models = tags_payload.get("models") if isinstance(tags_payload.get("models"), list) else []
    for m in models:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or "").strip()
        if not name:
            continue
        if name == want_clean:
            return True
        if name.startswith(want_clean + ":"):
            return True
        if _normalize_model_base(name) == want_base:
            return True
    return False


async def get_ollama_status(model_for_check: Optional[str] = None) -> Dict[str, Any]:
    """
    Probe Ollama HTTP API. Used before RCA and by GET /health/ollama.

    Returns keys: reachable, model_ready, model (configured), detail (optional error string).
    When ``model_for_check`` is set, readiness uses that name instead of ``OLLAMA_MODEL``.
    """
    want_model = (model_for_check if model_for_check is not None else settings.ollama_model).strip()
    base = settings.ollama_host.strip().rstrip("/")
    if not base:
        return {
            "reachable": False,
            "model_ready": False,
            "model": want_model,
            "detail": "ollama_host_empty",
        }

    url = urljoin(base + "/", "api/tags")
    timeout = httpx.Timeout(settings.ollama_health_timeout_seconds)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("ollama health GET /api/tags failed: %s", exc)
        return {
            "reachable": False,
            "model_ready": False,
            "model": want_model,
            "detail": str(exc),
        }

    if not isinstance(data, dict):
        return {
            "reachable": False,
            "model_ready": False,
            "model": want_model,
            "detail": "invalid_tags_json",
        }

    names: List[str] = []
    for m in data.get("models") if isinstance(data.get("models"), list) else []:
        if isinstance(m, dict) and m.get("name"):
            names.append(str(m["name"]))

    ready = _model_is_available(data, want_model)
    out: Dict[str, Any] = {
        "reachable": True,
        "model_ready": ready,
        "model": want_model,
        "models_installed": names[:50],
    }
    if not ready:
        out["detail"] = (
            f"model {want_model!r} not found in ollama; "
            f"run `ollama pull {want_model}` on the Ollama host"
        )
    return out


async def assert_ollama_ready_for_rca(model_name: Optional[str] = None) -> Optional[str]:
    """
    Return an error string if RCA should not call /api/chat; None if OK.
    Respects OLLAMA_HEALTH_REQUIRE_MODEL. Optional ``model_name`` overrides ``OLLAMA_MODEL`` for the check.
    """
    want = (model_name if model_name is not None else settings.ollama_model).strip()
    st = await get_ollama_status(model_for_check=want)
    if not st.get("reachable"):
        return st.get("detail") or "ollama_unreachable"
    if settings.ollama_health_require_model and not st.get("model_ready"):
        return st.get("detail") or "ollama_model_not_available"
    return None
