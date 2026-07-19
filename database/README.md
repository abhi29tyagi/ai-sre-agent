# Postgres (ai-sre-agent)

This folder holds **database-oriented documentation**. Schema bootstrap today lives in code (`app/db.py`), not checked-in `.sql` migrations—see below.

## What the DB is for

- Persist **Grafana webhook** payloads for replay, RCA rerun, and auditing.
- Store **Loki enrichment** and **Ollama RCA** output alongside each row (`JSONB`).

If Postgres is not configured, webhooks still process; **`event_id`** will be missing and enrichment is not stored—see logs on startup.

## Connection settings

The agent accepts either:

| Source | When to use |
|--------|-------------|
| **`DATABASE_URL`** | Full connection URI (set in `.env` / Bitbucket if you prefer one string). |
| **`POSTGRES_HOST`**, **`POSTGRES_PORT`**, **`POSTGRES_USER`**, **`POSTGRES_PASSWORD`**, **`POSTGRES_DB`** | Default in Compose; avoids URL-encoding issues if the password contains `@` or other special characters. |

`DATABASE_URL` wins when set (see `app/db.py` → `_build_conninfo`).

## Pool and lifecycle

- **`psycopg_pool.AsyncConnectionPool`** is opened on app startup and closed on shutdown (`app/main.py` lifespan).
- **`DB_POOL_MAX_SIZE`** (default `10`) caps concurrent connections.
- **`DB_CONNECT_TIMEOUT_SECONDS`** bounds the initial TCP connect.

## Schema (current)

Table **`incoming_alert_events`** is created/updated by **`ensure_events_table()`** in `app/db.py` on startup:

| Column | Notes |
|--------|--------|
| `id` | `BIGSERIAL` primary key (exposed as **event id** in Slack and `/events/…`). |
| `received_at` | Ingestion time (`TIMESTAMPTZ`, default `NOW()`). |
| `source` | e.g. `grafana`. |
| `status`, `alertname`, `group_key`, `fingerprint`, `org_id`, `alert_count` | Normalized from the first alert / payload. |
| `payload_json` | Full Grafana JSON (`JSONB`). |
| `loki_enrichment_json` | Loki + RCA metadata (`JSONB`); **added** via `ADD COLUMN IF NOT EXISTS` for upgrades. |

Indexes (also in `app/db.py`):

- `idx_incoming_alert_events_received_at` — recent listings (`/events/recent`).
- `idx_incoming_alert_events_fingerprint` — correlation by Grafana fingerprint.

**Source of truth for DDL:** `app/db.py` until standalone migration files are introduced.

## Operations

- **Backup:** snapshot the Postgres volume used by Compose (see main [README](../README.md) → Bitbucket / volume notes).
- **Inspect:** use `GET /events/recent` (with **`X-Webhook-Token`** when `WEBHOOK_SECRET` is set) or connect with `psql` using the same env as the agent.

## Roadmap

Richer incident schema and versioned migrations are still **TBD** (see main README status table). When migrations land, they will likely live under `database/migrations/` or similar; this README will be updated.
