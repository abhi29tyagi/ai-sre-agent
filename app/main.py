"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.config import settings
from app.db import close_db_pool, ensure_events_table, init_db_pool
from app.routes.events import router as events_router
from app.routes.health import router as health_router
from app.routes.slack_events import router as slack_events_router
from app.routes.webhooks import router as webhooks_router

logger = logging.getLogger(__name__)
logging.basicConfig(level=settings.log_level)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if not settings.webhook_secret:
        logger.warning(
            "WEBHOOK_SECRET is empty — POST /hooks/grafana and /events/* are open. "
            "Set WEBHOOK_SECRET in production."
        )
    await init_db_pool()
    await ensure_events_table()
    try:
        yield
    finally:
        await close_db_pool()


app = FastAPI(
    title="ai-sre-agent",
    description="Grafana webhooks, Loki enrichment, Ollama RCA, Slack digests (Phase 0–1).",
    version="0.4.0",
    lifespan=lifespan,
)


app.include_router(health_router)
app.include_router(events_router)
app.include_router(slack_events_router)
app.include_router(webhooks_router)
