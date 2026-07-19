"""Async Postgres pool + schema bootstrap for webhook intake persistence."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool

from .config import settings

logger = logging.getLogger(__name__)

_pool: Optional[AsyncConnectionPool] = None


def _build_conninfo() -> Optional[str]:
    """Pick DATABASE_URL when set, else build a conninfo from discrete POSTGRES_* vars."""
    if settings.database_url:
        return settings.database_url
    if not (settings.postgres_user and settings.postgres_password and settings.postgres_db):
        return None
    return make_conninfo(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password,
        dbname=settings.postgres_db,
        connect_timeout=settings.db_connect_timeout_seconds,
    )


async def init_db_pool() -> None:
    """Open the global connection pool (idempotent). No-op if DB is unconfigured."""
    global _pool
    if _pool is not None:
        return
    conninfo = _build_conninfo()
    if not conninfo:
        logger.warning(
            "database connection not configured; webhook events will not be persisted",
        )
        return
    _pool = AsyncConnectionPool(
        conninfo=conninfo,
        min_size=1,
        max_size=settings.db_pool_max_size,
        kwargs={"autocommit": True},
        open=False,
    )
    await _pool.open()
    logger.info("database pool opened max_size=%s", settings.db_pool_max_size)


async def close_db_pool() -> None:
    global _pool
    if _pool is None:
        return
    await _pool.close()
    _pool = None
    logger.info("database pool closed")


def db_pool() -> Optional[AsyncConnectionPool]:
    return _pool


@asynccontextmanager
async def get_connection() -> AsyncIterator[psycopg.AsyncConnection]:
    """Async context manager yielding a pooled connection (autocommit on)."""
    if _pool is None:
        raise RuntimeError("database pool not initialized")
    async with _pool.connection() as conn:
        yield conn


async def ensure_events_table() -> None:
    """Create intake table and indexes when missing (no-op without DB)."""
    if _pool is None:
        return
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS incoming_alert_events (
                    id BIGSERIAL PRIMARY KEY,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    source TEXT NOT NULL DEFAULT 'grafana',
                    status TEXT,
                    alertname TEXT,
                    group_key TEXT,
                    fingerprint TEXT,
                    org_id BIGINT,
                    alert_count INTEGER NOT NULL DEFAULT 0,
                    payload_json JSONB NOT NULL,
                    loki_enrichment_json JSONB
                );
                """
            )
            await cur.execute(
                "ALTER TABLE incoming_alert_events ADD COLUMN IF NOT EXISTS loki_enrichment_json JSONB;"
            )
            await cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_incoming_alert_events_received_at ON incoming_alert_events (received_at DESC);"
            )
            await cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_incoming_alert_events_fingerprint ON incoming_alert_events (fingerprint);"
            )
