"""Health route definitions."""

from typing import Any, Dict

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import settings
from app.services.ollama_health import get_ollama_status

router = APIRouter()


@router.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ollama")
async def health_ollama() -> Any:
    """Ollama daemon + model presence (GET /api/tags). Use for readiness before RCA traffic."""
    st = await get_ollama_status()
    ok = st.get("reachable") and (st.get("model_ready") or not settings.ollama_health_require_model)
    if not ok:
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "ollama": st},
        )
    return {"status": "ok", "ollama": st}
