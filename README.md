# ai-sre-agent

Orchestrator between **prod Grafana/Loki** and **operators + automation**: webhooks, future Loki enrichment, rules-first triage, LLM assist, Postgres, Slack/git PRs when you build them. **Ollama** runs in the sibling repo **`ai-model`**—not here.

**Full design:** [ARCHITECTURE.md](ARCHITECTURE.md)

---

## Start here (first deploy)

1. **Host:** CPU VM with **Docker Engine** and **Compose v2** (`docker compose version`). Enough RAM/disk for **Postgres + agent** plus **Ollama** (separate compose from `ai-model`).
2. **CI:** enable workflows/pipelines on **`ai-model`** and **`ai-sre-agent`**. Register **self-hosted runners** on this host (or split later if you want):
   - tags **`ai.model`** → runs **`deploy-ollama`**
   - tags **`ai.agent`** → runs **`deploy-agent`** (`scripts/deploy-agent.sh`)
3. **Secrets / env:** set the same names as [Deploy: deploy-agent](#deploy-deploy-agent) in CI secrets or the host environment (mark passwords/tokens secret).
4. **Deploy (order matters):** **`ai-model`** **`deploy-ollama`**, wait for success → on this repo run **`./scripts/deploy-agent.sh`**.
5. **Verify:** `curl -sS http://<vm-ip>:8080/health` → `{"status":"ok"}`. Then [Grafana webhook setup](#grafana-webhook-setup).
6. **Still missing for “full SRE agent” (beyond intake):** prod **Loki** read URL + auth, LogQL/label contract, Slack/git credentials, `OPENAI_*` if you skip Ollama for some paths, and policy/actions modules—all in [ARCHITECTURE.md](ARCHITECTURE.md) Phases **1+**.

---

## TO_DO tracker

Use this as a quick execution board for this repo.

| Item | Status |
|---|---|
| FastAPI service scaffolded and containerized (lifespan startup, async DB pool). | Done |
| `POST /hooks/grafana` ingress with constant-time **`WEBHOOK_SECRET`** check; same gate now applied to `/events/recent` and `/events/{id}/rca/rerun`. | Done |
| Webhook payload normalization + Postgres intake persistence (`incoming_alert_events`). | Done |
| `GET /events/recent` endpoint for quick intake verification. | Done |
| `POST /events/{id}/rca/rerun` — regenerate Ollama RCA (optional model, **`user_prompt`**, Slack resend). | Done |
| Slack Events API — thread **`rca` / `rerun`** → same rerun path; retries / duplicate `event_id` are dropped (in-memory TTL dedupe). | Done |
| Async Postgres via **`psycopg_pool.AsyncConnectionPool`** (no more sync DB calls inside async routes). | Done |
| Deploy script (`scripts/deploy-agent.sh`) with self-hosted runner support. | Done |
| Shared-host Compose collision fix via pinned project names in pipelines. | Done |
| Loki client (`query_range`), env routing, selector ladders, tight **`startsAt`** window then optional wide-span / **`now`** retries + asymmetric end-cap. | Done |
| Baseline redaction before Slack + Ollama (`redact_for_slack`, LLM blob redaction — extend patterns as needed). | Done |
| Slack Incoming Webhook digests, **Ollama RCA**, heuristic operator checklist; **smart checklist** for low-signal deliveries (e.g. DatasourceNoData, insufficient labels, total Loki transport failure). | Done |
| Grafana webhook **per-request overrides**: annotations / labels / `?slack_notify=` / `?ollama_rca=` (no redeploy). | Done |
| **`database/README.md`** — Postgres role, env, troubleshooting. | Done |
| Pytest suite for pure-function helpers (parser, signature, Loki window/normalize, dedupe, webhook overrides, operator checklist skip). | Done |
| Container hardening: non-root **`app`** user + agent **`/health`** healthcheck. | Done |
| Incident schema/migrations beyond raw intake event table. | Pending |
| Rule engine and confidence model for deterministic guidance. | Pending |
| Git forge PR flow (policy-gated, dry-run first). | Pending |
| Audit trail and rate limits for all external actions. | Pending |
| Optional: **re-query Loki on rerun** (refresh enrichment vs frozen snapshot). Still **not** implemented — rerun uses **stored** Loki enrichment only (`execute_rca_rerun`). | Pending |

---

## What this repository is

| | |
|--|--|
| **For** | **FastAPI** service: Grafana/Alertmanager **webhooks**, **`/health`**, later **Loki**, rules, LLM calls, notifications, optional PRs. **Postgres** for incidents and dedupe. |
| **Not for** | **Ollama** containers (→ **`ai-model`**), hosting **Grafana/Loki/Alloy**, or your **application** source repos. |
| **Stack** | Default local LLM: **Ollama** on same VM via **`OLLAMA_HOST`**. Stronger models: **`OPENAI_*`** vendor APIs. GPU **vLLM** later: **`ai-model`** + **`deploy-vllm`** + runner **`common.ai`**. |

---

## Contents of this repo (git)

| Path | Role |
|------|------|
| **`app/main.py`** | FastAPI entrypoint: lifespan-managed DB pool + table bootstrap, router registration. |
| **`app/config.py`** | Environment-backed settings (`DATABASE_URL`, `WEBHOOK_SECRET`, timeouts, log level). |
| **`app/db.py`** | Async **`psycopg_pool`** wrapper + table/index bootstrap (`incoming_alert_events`). |
| **`app/services/intake.py`** | Async payload normalization, persistence, recent/by-id event fetch. |
| **`app/services/loki.py`** | Loki `query_range` client, selector ladder, time-window math, error pass + stratify. |
| **`app/services/ollama_rca.py`** | Builds the RCA prompt and calls Ollama `/api/chat`. |
| **`app/services/slack_*.py`** | Incoming Webhook digest, signature verify, in-memory event dedupe. |
| **`app/services/rca_rerun.py`** | Shared rerun path used by HTTP and Slack Events triggers. |
| **`app/routes/`** | Route modules (`health`, `events`, `webhooks`, `slack_events`) split by concern. |
| **`tests/`** | Pytest suite for pure-function helpers (no DB / Ollama needed). |
| **`requirements.txt`**, **`requirements-dev.txt`**, **`Dockerfile`** | Python deps and agent image build (non-root user, healthcheck-friendly). |
| **`docker-compose.yml`** | **Postgres** + **agent** only (agent has a `/health` healthcheck). |
| **`.env.example`** | Env names for local use; pipeline writes a real **`.env`** on the runner (never commit secrets). |
| **`scripts/deploy-agent.sh`** | Host/CI deploy for Compose project **`ai_sre_agent`**. |
| **`ARCHITECTURE.md`** | Phases, security, Loki/Grafana contracts, diagrams. |
| **[`database/README.md`](database/README.md)** | Postgres: connection env, pool, `incoming_alert_events` schema, operations. |
| **`docs/`**, **`scripts/`** | Mermaid sources, PNG diagrams, `./scripts/render-architecture-diagrams.sh`. |

---

## Runtime (after `deploy-agent`)

| Piece | Role |
|-------|------|
| **`agent` container** | HTTP API (health + webhook + recent-events view), payload normalization, and intake persistence to Postgres; later: Loki client, policy, LLM, Slack, git PRs. |
| **`postgres` container** | Durable data; **`incoming_alert_events`** — details in [**database/README.md**](database/README.md). |
| **Ollama** | Separate project in **`ai-model`**; agent reaches it via **`OLLAMA_HOST`**. |
| **Prod Grafana / Loki** | External; Grafana **POSTs in**, agent will **query Loki out** once implemented. |

**Roadmap modules** (Loki enricher, redaction, policy, LLM orchestration, Slack, git PRs, etc.) are specified in [ARCHITECTURE.md](ARCHITECTURE.md). Current code covers **ingress + auth + basic normalization + Postgres intake persistence**.

---

## Data flow (same CPU VM)

```text
[Apps] → [Alloy] → [Loki] ↔ [Grafana alerts]
                          │ webhook
                          ▼
              ┌─────────────────────┐   ┌──────────────────┐
              │  ai-sre-agent       │   │  ai-model        │
              │  FastAPI + Postgres │   │  Ollama :11434   │
              └──────────┬──────────┘   └────────┬─────────┘
                         │    OLLAMA_HOST       │
                         └──────────────────────┘
        Slack / git PRs / vendor OPENAI_*  →  later phases
```

---

## Grafana webhook setup

Grafana lives in **prod** (not this repo). Point it at **`https://<host>:<port>/hooks/grafana`** (or `http://` on private nets if policy allows).

**Before you configure Grafana:** agent must answer **`GET /health`** from the same network path Grafana will use.

1. **Alerting → Contact points → Add contact point** → integration **Webhook**.
2. **URL:** `https://<agent-host>:8080/hooks/grafana` (adjust host/port/TLS terminator).
3. **Method:** **POST**.
4. **Auth (recommended in prod):** set **`WEBHOOK_SECRET`** on the agent and add header **`X-Webhook-Token: <same value>`** on the Grafana contact point. The same token also gates **`/events/recent`** and **`/events/{id}/rca/rerun`**. Comparison is constant-time (`hmac.compare_digest`). When **`WEBHOOK_SECRET`** is empty the agent logs a startup WARNING and the routes accept any caller — fine for local dev, never for prod.
5. **Alerting → Notification policies** (or per-rule **Notifications**) to route a **small test set** of alerts first.
6. **Test** from Grafana; on the VM run **`docker compose logs -f agent`** and confirm **`grafana webhook received`** with an **`event_id`**.
7. **Alertmanager:** same idea—**webhook** receiver pointing at the same URL/headers; payload shape may differ until you add normalization code.

### Loki enrichment (prod vs non-prod)

You can run **two Loki instances** (for example prod and non-prod). The agent picks **`LOKI_URL_PROD`** vs **`LOKI_URL_NONPROD`** using **Grafana alert labels first**, then **annotations** if labels omit env (same keys: optional **`loki_env`** / **`loki_stack`**, then `alloy_environment`, `environment`, `env`, `stage`). Add **`loki_env=prod`** (or **`staging`**, etc.) on the **alert rule** if you want routing that does not depend on log stream labels. That decides **which Loki API** to call **before** `query_range`; LogQL stream selectors still use labels such as `environment` / `service_name` to match lines.

If Grafana omits env labels, set **`LOKI_URL_DEFAULT`** as a fallback (optional). Configure **`LOKI_URL_PROD`**, **`LOKI_URL_NONPROD`**, and optionally **`LOKI_BEARER_TOKEN`** (see `.env.example` and [Deploy: deploy-agent](#deploy-deploy-agent)). Toggle enrichment with **`LOKI_ENRICH_ENABLED=false`** if you need webhooks without calling Loki.

If your Explore queries look like **`{service_name="$app"} |= "…" | logfmt`**, note the **primary** enrichment pass runs **`query_range` on stream selectors only** (no `logfmt`). An **optional second pass** appends LogQL **`|~`** (**`LOKI_ENRICH_ERROR_LOGQL_REGEX`**) and merges deduped lines with the broad pass **before** the line-cap. Some stacks only put **`service_name`** on the stream, not **`environment`** — enrichment tries narrower selectors first, then **`{service_name="…"}`** alone so it lines up with how you browse in Grafana.

**Loki `query_range` uses log entry index timestamps** (same timeline as Grafana Explore), not human-readable times embedded in Nest log lines. Enrichment first queries a **tight** window around Grafana **`startsAt`**: **from** `startsAt − LOKI_ENRICH_WINDOW_MINUTES` **to** `min(now, startsAt + LOKI_ENRICH_AFTER_START_MINUTES)` when **now ≥ startsAt**, else **`startsAt + after`** when **now < startsAt** — note **`anchor_startsAt_skew_tight`** when **`startsAt`** is ahead of agent **now** and gap ≤ **`LOKI_ENRICH_FUTURE_GAP_SPAN_MAX_MINUTES`**. If the gap is **wider**, see **`anchor_startsAt_future_gap_wide_use_startsAt_not_agent_now`**. If the **tight** query returns **no streams** and skew applies, the agent may **retry** with a **wide** bridge window (**`LOKI_ENRICH_RETRY_WIDE_SPAN_ON_EMPTY`**, default **true**; note **`loki_retry_wide_span_after_empty_tight`**). If still empty, **retry** with a **bridge window** spanning agent **now** and Grafana **`startsAt`** (**`LOKI_ENRICH_EMPTY_FALLBACK_TO_NOW`** — the name is historical; it no longer queries only around agent clock). When a later pass wins, **`window_primary_attempt`** stores the **tight** window. Set **`LOKI_ENRICH_CLAMP_FUTURE_STARTS_AT=false`** for strict **`startsAt`** rules without skew bridging. To ignore **`startsAt`**, use **`LOKI_ENRICH_ANCHOR=now`**.

**Root-cause context (INFO vs ERROR):** the broad pass does **not** filter by log level; the **error pass** adds a regex filter (see above). Under **`LOKI_RESULT_MAX_TOTAL_LINES`**, **`LOKI_ENRICH_PRIORITIZE_STARTS_AT`** picks lines near Grafana **`startsAt`**; **`LOKI_ENRICH_STRATIFY_LINES`** splits the cap into **before** vs **after** **`startsAt`** (**`LOKI_ENRICH_LINE_BUDGET_BEFORE_PCT`**, unused slots spill). **backward** still returns newest-first from Loki; for strict chronological lead-up, set **`LOKI_QUERY_DIRECTION=forward`**, widen **`LOKI_ENRICH_WINDOW_MINUTES`**, and raise **`LOKI_RESULT_MAX_TOTAL_LINES`**. Optional: increase **`LOKI_ENRICH_AFTER_START_MINUTES`** if recovery matters.

### Ollama RCA sampling: temperature and top_p

**Why:** **`temperature`** and **`top_p`** are **decoding controls** for `POST /api/chat`: they change how random the next tokens are. They are **not** model-reported “confidence” scores. For on-call RCA, **lower temperature** (often **0.2–0.5**) usually yields **steadier, less speculative** wording when logs are sparse; defaults leave sampling to Ollama.

**How:** Set optional **`OLLAMA_RCA_TEMPERATURE`** and **`OLLAMA_RCA_TOP_P`** in the environment (see **`.env.example`**). If **unset**, those keys are **omitted** from the request body and **Ollama server defaults** apply. If set, the agent sends **`"options": { "temperature": …, "top_p": … }`** on **`/api/chat`**. Stored RCA may include **`request_options`** in the **`rca_ollama`** object so you can audit what was used.

### Slack (optional)

Create an [**Incoming Webhook**](https://api.slack.com/messaging/webhooks) and set **`SLACK_WEBHOOK_URL`** (mark it secret in CI / `.env`). Each **`POST /hooks/grafana`** sends: environment + alert, **Ollama RCA** (if **`OLLAMA_RCA_ENABLED`** and **`OLLAMA_HOST`** reach your **`ai-model`** Ollama), **rule-based checklist**, and **redacted** log snippets. Tune **`OLLAMA_MODEL`** to match `ollama pull` on the host. Disable Slack with **`SLACK_NOTIFY_ENABLED=false`**; disable RCA only with **`OLLAMA_RCA_ENABLED=false`**. Response JSON includes **`loki_enrichment.rca`** (preview), **`slack`** metadata (optional **`operator_checklist_skipped`** when generic bullets were replaced), and **`webhook_notify`** (effective flags after overrides).

**Smart operator checklist:** when the delivery is **low-signal** (e.g. **`DatasourceNoData`**, Loki skip **`insufficient_labels_for_logql_selector`**, no Loki URL / enrichment disabled, or **every** `query_range` attempt failed with network/HTTP errors), the agent **replaces** the generic LOKI_* bullet list with **one short explanation**—no Grafana rule changes. Set **`SLACK_SMART_SKIP_OPERATOR_CHECKLIST=false`** to always emit the full heuristic list.

**Per-request Slack / RCA (no env redeploy):** Grafana cannot change the agent’s env vars when an alert fires, but you can turn off Slack posts or Ollama for **that webhook** in two ways — precedence is **alert annotation → alert label → URL query → env**.

| Mechanism | Example |
|-----------|---------|
| **Alert annotation** (recommended on noisy rules) | Add custom annotations **`ai_sre_notify_slack`** = `false` and/or **`ai_sre_ollama_rca`** = `false` (`true` / `false` / `1` / `0`). |
| **Alert label** | Same keys on **custom labels** if you prefer; annotation wins when both set the same key. |
| **Webhook URL query** | Contact point URL: `https://<agent>/hooks/grafana?slack_notify=false&ollama_rca=false` (use a **second** contact point for “quiet” rules only). |

The HTTP **`200`** JSON echoes **`webhook_notify`: `{ "slack": …, "ollama_rca": … }`** so you can confirm overrides applied. Skipped RCA stores **`skipped_reason`: `ollama_rca_webhook_override`** when Ollama was disabled by override (vs **`ollama_rca_disabled`** from env).

#### Slack Events API — rerun RCA from a thread reply

Incoming Webhooks only **send** messages to Slack. To **receive** commands (e.g. reply in thread), add a Slack app with **Event Subscriptions**:

| Step | Action |
|------|--------|
| 1 | Same or separate Slack app → **Event Subscriptions** → enable. |
| 2 | **Request URL:** `https://<public-host>/hooks/slack/events` (HTTPS). Use a **Cloudflare Tunnel** host (e.g. `https://ai-sre-agent.example.com/...` from [Cloudflare Tunnel](#cloudflare-tunnel-optional-public-https)) or any reachable reverse proxy. Save — Slack sends a **challenge**; the agent answers automatically. |
| 3 | Under **Subscribe to bot events**, add **`message.channels`** (add **`message.groups`** for private channels, **`message.im`** for DMs if needed). |
| 4 | **OAuth & Permissions** → **Bot Token Scopes:** **`channels:history`** (and **`groups:history`** for private channels). For thread replies after **`rca`** / **`rerun`**, add **`chat:write:bot`**. Optional **`channels:join`** so the bot can join **public** channels automatically when **`chat.postMessage`** returns **`channel_not_found`** / **`not_in_channel`**. **Private** channels: **`/invite @YourApp`** in that channel. **Reinstall** the app after scope changes. |
| 5 | **Basic Information** → **Signing Secret** → **`SLACK_SIGNING_SECRET`** on the agent (secret in CI / `.env`). |
| 6 | **OAuth & Permissions** → **Bot User OAuth Token** (`xoxb-`) → **`SLACK_BOT_TOKEN`** (optional). Without it, reruns still execute but nothing is posted back into Slack. |
| 7 | `/invite @YourApp` in the alert channel so the app can see messages. |

Under the digest message, **reply in the thread** (not top-level) with:

`rca 19` · `rerun 19` · `rca 19 llama3.1` · `rerun 19 -- Focus on Mongo and replication lag` · `rerun 19 llama3.2 -- narrow to customer impact`

**`rca` and `rerun` are the same command** — both **re-run Ollama** on the **already-stored** event (same Grafana payload + Loki enrichment in Postgres). You are not replaying the Grafana webhook; you are **regenerating RCA text** on that snapshot. Use the same numeric **event id** as in the digest header (`event \`N\``). Optional **model** is one token (`llama3.2`, `mistral`, etc.) and must exist in Ollama. Optional **operator instructions** go after **`--`** (or write a multi-word message without **`--`** — everything after the event id is treated as instructions unless it is a single model-looking token). Instructions are appended to the RCA prompt (length-capped by **`OLLAMA_RCA_USER_PROMPT_MAX_CHARS`**).

Thread replies use the same path as **`POST /events/{id}/rca/rerun`**. Whether a **second full Incoming Webhook digest** is posted after a rerun is controlled by **`NOTIFY_SLACK_ON_RCA_RERUN`** (CI / env, default **false**). The HTTP body field **`notify_slack`** overrides env when you send **`true`** or **`false`** explicitly; omit the field to follow env.

### Quick checks with GET (curl)

From any machine that can reach the agent (replace **`HOST`** and **`PORT`**; default **`PORT`** is **8080**):

```bash
curl -sS "http://HOST:8080/health"
```

Expected: `{"status":"ok"}`.

Ollama readiness (daemon + pulled model, same check RCA runs before **`/api/chat`**):

```bash
curl -sS -o /dev/null -w "%{http_code}" "http://HOST:8080/health/ollama"
```

Expect **200** when Ollama responds and **`OLLAMA_MODEL`** is installed; **503** if down or model missing (see JSON body).

List recent persisted webhook rows (default **`limit=20`**, max **`200`**):

```bash
curl -sS "http://HOST:8080/events/recent?limit=10"
```

**Rerun Ollama RCA** on a persisted event (same stored Loki lines + Grafana payload; does **not** re-query Loki). Uses **`OLLAMA_MODEL`** unless you pass **`model`**. Auth matches Grafana: if **`WEBHOOK_SECRET`** is set, send **`X-Webhook-Token`**.

```bash
curl -sS -X POST "http://HOST:8080/events/18/rca/rerun" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Token: $WEBHOOK_SECRET" \
  -d '{"model": "llama3.2", "user_prompt": "Emphasize blast radius and whether rollback is warranted.", "notify_slack": true}'
```

Omit **`X-Webhook-Token`** when the agent has no **`WEBHOOK_SECRET`**. Omit **`notify_slack`** in the JSON to use **`NOTIFY_SLACK_ON_RCA_RERUN`** from the environment, or set **`"notify_slack": true`** to force a fresh Block Kit digest for that call. For **Slack-native** triggers without curl, use **[Slack Events API](#slack-events-api-rerun-rca-from-a-thread-reply)** (thread reply) or a **Workflow** HTTP step.

**Interactive API docs** (FastAPI): open **`http://HOST:8080/docs`** in a browser (Swagger UI) or **`/redoc`**.

---

## Tests

Pure-function tests live in **`tests/`** and don't need Postgres or Ollama. Install dev deps and run:

```bash
pip install -r requirements-dev.txt
pytest
```

Suites cover the rerun command parser, Slack signature verification, Slack Incoming Webhook URL normalization, Loki time-window math, Loki line-cap stratify, and the in-memory Slack-event dedupe. Add a new test alongside any change to those helpers.

---

## Deploy: deploy-agent

### Prerequisites

- Self-hosted runner (or SSH session) with Docker + Compose v2; optional runner tag **`ai.agent`**.
- **`deploy-ollama`** has already run on this host with project **`ai_model_ollama`** so **`ai_model_ollama_default`** exists and **ollama** listens on port **11434** inside that network (unless you override **`OLLAMA_HOST`**).

### Docker networking (`ai_model_ollama_default`)

**`ai-model`** **`deploy-ollama`** uses **`docker compose -p ai_model_ollama`**, which creates the default network **`ai_model_ollama_default`**. This repo’s **`docker-compose.yml`** attaches only the **agent** to that network as **external** (no **`ai-model`** changes). Default **`OLLAMA_HOST=http://ollama:11434`** uses the Compose **service** name on that network. Postgres stays on **`ai_sre_agent_default`** only.

Run **`deploy-ollama`** before **`deploy-agent`** so the external network exists (the pipeline fails fast otherwise). Validate with **`GET /health/ollama`**.

If you cannot share the network (e.g. Ollama elsewhere), remove the **`ai_model_ollama_default`** attachment from **`docker-compose.yml`** and set **`OLLAMA_HOST`** to a reachable URL (e.g. **`http://host.docker.internal:11434`**).

### Cloudflare Tunnel (optional, public HTTPS)

Set **`CLOUDFLARE_TUNNEL_ENABLED=true`** and **`CLOUDFLARE_TUNNEL_TOKEN`** (connector token from Zero Trust → tunnel → **Install connector**). This repo only runs **`cloudflared`** with that token; **create the tunnel, public hostname, and DNS/CNAME in Cloudflare** (one-time in the dashboard).

**Public hostname** (e.g. `https://ai-sre-agent.example.com`) → in Zero Trust add **Public hostname** pointing to **`http://localhost:8080`** (**`cloudflared`** uses **`network_mode: service:agent`**).

**Env for tunnel:** **`CLOUDFLARE_TUNNEL_ENABLED`**, **`CLOUDFLARE_TUNNEL_TOKEN`** (secret) — nothing else.

Local compose with tunnel:

```bash
docker compose -p ai_sre_agent -f docker-compose.yml -f docker-compose.cloudflare.yml \
  --env-file .env --profile cloudflare up -d
```

Put **`CLOUDFLARE_TUNNEL_TOKEN=...`** in **`.env`** (pipeline appends it when tunnel is enabled).

### Repository variables

Set as **CI secrets**, host environment, or a local **`.env`** (never commit secrets). `./scripts/deploy-agent.sh` writes `.env` from the process environment.

| Variable | Required | Secret? | Purpose |
|----------|----------|----------|---------|
| **`POSTGRES_PASSWORD`** | **Yes** | Yes | Postgres superuser password for Compose. |
| **`WEBHOOK_SECRET`** | No | Yes | If set, Grafana must send **`X-Webhook-Token`** with the same value. |
| **`POSTGRES_USER`** | No | No | Default **`sre`**. |
| **`POSTGRES_DB`** | No | No | Default **`sre_agent`**. |
| **`POSTGRES_HOST`** | No | No | Default **`postgres`** (service name in compose). |
| **`POSTGRES_PORT`** | No | No | Default **`5432`**. |
| **`AGENT_PORT`** | No | No | Host port published for FastAPI (default **8080**). |
| **`LOG_LEVEL`** | No | No | Default **`INFO`**. |
| **`OLLAMA_HOST`** | No | No | **Ollama** HTTP API (`/api/chat` for RCA); default **`http://ollama:11434`** via external network **`ai_model_ollama_default`**. |
| **`OLLAMA_MODEL`** | No | No | Model name on the host (e.g. **`llama3.2`**); must be pulled (`ollama pull`). |
| **`OLLAMA_RCA_ENABLED`** | No | No | Default **`true`** — RCA after Loki enrichment; stored under **`rca_ollama`**. |
| **`OLLAMA_RCA_TEMPERATURE`** | No | No | Optional. Passed as **`options.temperature`** on **`/api/chat`**; omit to use Ollama defaults. Lower → less random completions (see [above](#ollama-rca-sampling-temperature-and-top_p)). |
| **`OLLAMA_RCA_TOP_P`** | No | No | Optional. Passed as **`options.top_p`** on **`/api/chat`**; omit to use Ollama defaults. |
| **`OLLAMA_TIMEOUT_SECONDS`** | No | No | Default **`120`** (CPU inference can be slow). |
| **`OLLAMA_RCA_MAX_LOG_CHARS`** | No | No | Max redacted log chars in the RCA prompt (default **12000**). |
| **`OLLAMA_RCA_USER_PROMPT_MAX_CHARS`** | No | No | Max length for optional **`user_prompt`** on RCA rerun (Slack **`rerun N -- …`** / **`POST …/rca/rerun`**) — default **2000**. |
| **`OLLAMA_HEALTH_TIMEOUT_SECONDS`** | No | No | Timeout for **`GET /api/tags`** preflight (default **5**). |
| **`OLLAMA_HEALTH_REQUIRE_MODEL`** | No | No | Default **`true`** — RCA requires **`OLLAMA_MODEL`** to appear in **`ollama list`**. Set **`false`** if only the daemon must be up. |
| **`LOKI_URL_PROD`** | No | No | Base URL for prod Loki (`query_range`). |
| **`LOKI_URL_NONPROD`** | No | No | Base URL for non-prod Loki. |
| **`LOKI_URL_DEFAULT`** | No | No | Used when alert env labels are missing or ambiguous. |
| **`LOKI_BEARER_TOKEN`** | No | Yes | Optional `Authorization: Bearer …` for Loki HTTP API. |
| **`LOKI_SCOPE_ORG_ID`** | No | No | Set if your Grafana Loki datasource uses **`X-Scope-OrgID`** (multi-tenant); must match or queries return **no streams**. |
| **`LOKI_ENRICH_ENABLED`** | No | No | Default **`true`**. Set **`false`** to skip Loki calls on webhook. |
| **`LOKI_ENRICH_WINDOW_MINUTES`** | No | No | Minutes **before** **`startsAt`** included in the window (default **10** → **startsAt − 10** …). |
| **`LOKI_ENRICH_AFTER_START_MINUTES`** | No | No | Minutes **after** **`startsAt`** for the window **end** cap: **`min(now, startsAt + after)`** (default **5**). Prevents backward sampling from hugging wall-clock **now**. |
| **`LOKI_ENRICH_ANCHOR`** | No | No | **`alert_start`** (default) uses Grafana **`startsAt`**; **`now`** uses ingestion time. |
| **`LOKI_ENRICH_CLAMP_FUTURE_STARTS_AT`** | No | No | Default **`true`**. Uses **tight** **`startsAt`** windows when **`startsAt` > now+2m**; wide span is an **optional retry** (see **`LOKI_ENRICH_RETRY_WIDE_SPAN_ON_EMPTY`**). Set **`false`** to disable skew bridging behavior. |
| **`LOKI_ENRICH_FUTURE_GAP_SPAN_MAX_MINUTES`** | No | No | Default **180**. Max **`startsAt` − now** gap (minutes) for allowing the **wide span retry** after an empty tight query. |
| **`LOKI_ENRICH_RETRY_WIDE_SPAN_ON_EMPTY`** | No | No | Default **`true`**. After an empty **tight** window, retry with the wide **now↔startsAt** span (when gap ≤ **`LOKI_ENRICH_FUTURE_GAP_SPAN_MAX_MINUTES`**). |
| **`LOKI_ENRICH_EMPTY_FALLBACK_TO_NOW`** | No | No | Default **`true`**. If still no streams and **`startsAt` > now+2m**, retry with a **bridge** window (see above) that includes both agent **now** and Grafana **`startsAt`** so a lagging host does not miss the alert time. |
| **`LOKI_ENRICH_PRIORITIZE_STARTS_AT`** | No | No | Default **`true`**. Prefer lines whose **Loki index time** is closest to **`startsAt`** within the cap (reduces **backward** newest-first bias). |
| **`LOKI_ENRICH_STRATIFY_LINES`** | No | No | Default **`true`**. Reserve **`LOKI_ENRICH_LINE_BUDGET_BEFORE_PCT`** of the cap for lines **before** **`startsAt`**; remainder after; spill if one side is sparse. |
| **`LOKI_ENRICH_LINE_BUDGET_BEFORE_PCT`** | No | No | **0–100** (default **50**). Percent of **`LOKI_RESULT_MAX_TOTAL_LINES`** for **lead-up** (before **`startsAt`**). |
| **`LOKI_ENRICH_ERROR_PASS_ENABLED`** | No | No | Default **`true`**. Second **`query_range`** with **`|~`** regex; merged with the broad pass before the cap. |
| **`LOKI_ENRICH_ERROR_LOGQL_REGEX`** | No | No | RE2 regex for **`|~`** (default matches common error wording). |
| **`LOKI_QUERY_DIRECTION`** | No | No | **`backward`** (default) = newest lines first; **`forward`** = oldest first in the window (often better to see INFO lead-up before ERROR). |
| **`LOKI_QUERY_LIMIT`** | No | No | Lines requested per Loki **`query_range`** call (default **500**). Raise for chatty services that hit the cap before the alert moment. |
| **`LOKI_RESULT_MAX_TOTAL_LINES`** | No | No | Max lines stored per enrichment (default **100**). Raise for more mixed-level context. |
| **`DB_POOL_MAX_SIZE`** | No | No | psycopg async pool size (default **10**). Raise under sustained webhook traffic. |
| **`SLACK_WEBHOOK_URL`** | No | Yes | Slack Incoming Webhook URL; digest sent after each Grafana webhook when set. |
| **`SLACK_NOTIFY_ENABLED`** | No | No | Default **`true`** when webhook URL present; set **`false`** to disable posting. |
| **`SLACK_SMART_SKIP_OPERATOR_CHECKLIST`** | No | No | Default **`true`**. If **`true`**, replace generic operator bullets with one line for no-data / missing labels / Loki transport failures. |
| **`SLACK_SIGNING_SECRET`** | No | Yes | Slack Events API signature verification for **`POST /hooks/slack/events`** (thread **`rca`** / **`rerun`** commands). |
| **`SLACK_BOT_TOKEN`** | No | Yes | Bot **`xoxb-`** token; optional thread acks via **`chat.postMessage`** — Bot scopes must include **`chat:write:bot`** (not covered by Incoming Webhooks alone). |
| **`NOTIFY_SLACK_ON_RCA_RERUN`** | No | No | Default **`false`**. If **`true`**, rerun (**HTTP** or Slack **`rca`**/**`rerun`**) posts a **full Block Kit digest** to **`SLACK_WEBHOOK_URL`** after RCA. Omit **`notify_slack`** in rerun JSON to use this; **`notify_slack`** in the body overrides per request. |
| **`CLOUDFLARE_TUNNEL_ENABLED`** | No | No | Set **`true`** to run **`cloudflared`** (see [Cloudflare Tunnel](#cloudflare-tunnel-optional-public-https)). |
| **`CLOUDFLARE_TUNNEL_TOKEN`** | No | Yes | Tunnel connector token from Zero Trust (**secured**). Required when tunnel enabled. |

The app now uses discrete Postgres env vars (host/user/password/db) by default, which avoids URL parsing issues when passwords include special characters like `@`.

**Not injected by this pipeline yet (set later in Compose/env when you wire them):** `OPENAI_BASE_URL`, `OPENAI_API_KEY`—see [ARCHITECTURE.md](ARCHITECTURE.md).

### How to deploy

1. Set the table above in the environment (at minimum **`POSTGRES_PASSWORD`**).
2. On the VM or a self-hosted runner with Docker: **`./scripts/deploy-agent.sh`**.
3. Any CI system can call that script after checkout; keep **`ai-model` `deploy-ollama`** first so **`ai_model_ollama_default`** exists.

### If Docker fails with `tcp://localhost:2375`

The runner process may inherit **`DOCKER_HOST=tcp://localhost:2375`** (common in cloud CI examples). **Self-hosted** Linux should talk to **`/var/run/docker.sock`** instead. The pipeline runs **`unset DOCKER_HOST`** before compose; if it still fails:

- As the **same OS user** the deploy job uses: **`docker info`** and **`docker compose version`** must work.
- Add that user to the **`docker`** group and **restart** the runner service, or install the runner so it uses a user that already has socket access.

### If `deploy-ollama` stops the agent containers

If both repos run on the same host and you see agent containers shut down during model deploy, that is usually a **Compose project-name collision** (both jobs defaulted to project `build` and used `--remove-orphans`).

This repo now pins project name **`ai_sre_agent`** in pipeline commands. `ai-model` pins **`ai_model_ollama`** and **`ai_model_vllm`** so stacks are isolated.

To preserve existing data from earlier `build_*` deployments, the Compose volume is pinned to **`build_postgres_data`**.

---

## Related repo

**`ai-model`** — **`deploy-ollama`** (runner **`ai.model`**) for Ollama; **`deploy-vllm`** (runner **`common.ai`**, GPU host) for vLLM later. Use that repository’s README for variables and prerequisites.

---

## More documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — phases, security, Loki label/LogQL prerequisites, PNG diagrams  
- [docs/diagrams/mermaid/](docs/diagrams/mermaid/) — diagram sources  
- [docs/diagrams/](docs/diagrams/) — rendered PNGs  
