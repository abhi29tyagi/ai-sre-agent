"""Root-cause analysis via local Ollama HTTP API (`/api/chat`)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import httpx

from app.config import settings
from app.services.ollama_health import assert_ollama_ready_for_rca
from app.services.suggestions import enrichment_log_lines, redact_for_slack

logger = logging.getLogger(__name__)


def _redact_for_llm(text: str) -> str:
    s = redact_for_slack(text, max_len=settings.ollama_rca_max_log_chars + 5000)
    s = re.sub(r"\b[\w._%+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[email]", s)
    return s


_ERROR_KEYWORDS_RE = re.compile(settings.loki_enrich_error_logql_regex)

# Substrings that indicate sample-logging-app (or similar) intentional demo traffic — drives a final REMINDER block.
_SIMULATED_ROUTE_HINT_RE = re.compile(
    r'/api/simulate-error|simulated\s+error\s+path|"simulated"\s*:\s*true',
    re.IGNORECASE,
)


def _excerpt_has_error_signals(log_blob: str) -> bool:
    """Match the same regex Loki used for its error-focused pass.

    When this is False, the Loki window has no error/warn/exception lines and the model must
    not invent failure types — the RCA should report 'no error-level lines in this window'.
    """
    if not log_blob:
        return False
    return bool(_ERROR_KEYWORDS_RE.search(log_blob))


def _excerpt_looks_simulated_demo_route(log_blob: str) -> bool:
    if not log_blob.strip():
        return False
    return bool(_SIMULATED_ROUTE_HINT_RE.search(log_blob))


def _unescape_json_string_chunk(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\")


def _extract_simulated_err_message(blob: str) -> Optional[str]:
    """Best-effort parse of ``err.message`` from JSON log lines containing simulate-error / simulated."""
    if not blob.strip():
        return None
    focused = [ln for ln in blob.splitlines() if _SIMULATED_ROUTE_HINT_RE.search(ln)]
    haystack = "\n".join(focused) if focused else blob
    m = re.search(
        r'"err"\s*:\s*\{.*?"message"\s*:\s*"((?:[^"\\]|\\.)*)"',
        haystack,
        re.DOTALL,
    )
    if m:
        return _unescape_json_string_chunk(m.group(1))
    m = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', haystack)
    if m:
        return _unescape_json_string_chunk(m.group(1))
    return None


def _render_simulated_demo_rca(err_message: str) -> str:
    """Fixed Slack mrkdwn RCA for intentional `/api/simulate-error` traffic (sample-logging-app and similar)."""
    return (
        "*Likely cause*\n\n"
        f"{err_message} Logged on GET `/api/simulate-error` with `simulated: true`; HTTP 500 is expected from this "
        "demo handler, not a customer-facing outage or real dependency failure.\n\n"
        "*Evidence*\n\n"
        "- ERROR record includes the `err.message` above and `res.statusCode` 500 for `/api/simulate-error`.\n"
        "- If pino-http emits two ERROR lines for one request, use the first line's `err` object; the "
        "'failed with status code 500' line is logger noise.\n\n"
        "*Next steps*\n\n"
        "- Exclude or filter `/api/simulate-error` in the alert LogQL / matcher, or mute/narrow the rule if this is "
        "expected test traffic.\n"
        "- Run intentional demo calls outside paging hours if the route must stay exposed in prod.\n"
    )


def _final_simulated_route_reminder_lines() -> List[str]:
    """Last lines in the user prompt — recency bias for small models."""
    return [
        "",
        "FINAL REMINDER — this excerpt matches **intentional demo / simulate-error** signals (above rules still apply):",
        "- *Likely cause* sentence 1 must **quote or restate err.message** from the ERROR JSON (e.g. text inside "
        "`\"message\":` under `err`) — **not** 'The primary issue appears to be a simulated error in the … path'.",
        "- *Evidence*: **never** one giant bullet chaining `req.id: … - simulated: … - res.statusCode:`; **never** "
        "`XXXXXXXX`; **never** a bullet that only says DB/upstream is absent.",
        "- *Next steps*: route/url is **already in the excerpt** — **forbid** "
        "'Identify the specific route', 'examine Grafana logs to find the route', 'verify … is the source not a "
        "downstream issue', 'confirm downstream vs simulate-error'. Those ask on-call to rediscover what logs already "
        "show. Use: exclude `/api/simulate-error` from the alert/matcher, mute/narrow the rule, or schedule demo calls "
        "outside paging.",
        "",
    ]


def _build_user_prompt(
    summary: Dict[str, Any],
    log_blob: str,
    *,
    operator_instructions: str | None = None,
) -> str:
    """Assemble the user message for `/api/chat`: alert metadata, log blob, branch rules, then shared discipline.

    ``summary`` comes from the alert envelope; ``log_blob`` is redacted enrichment. Rules repeat across the CRITICAL
    branch, analysis sections (A–C), formatting, and Slack readability — intentional overlap to reduce small-model
    drift; trim carefully and keep ``tests/test_rca_prompt_rules.py`` substrings in sync.
    """
    has_error_signals = _excerpt_has_error_signals(log_blob)
    lines: List[str] = [
        "You are assisting with production incident triage.",
        "",
        f"Alert name: {summary.get('alertname') or 'unknown'}",
        f"Status: {summary.get('status')}",
        f"Environment (from routing): {summary.get('environment') or 'unknown'}",
        f"Fingerprint: {summary.get('fingerprint')}",
        f"Grafana annotation summary: {summary.get('annotation_summary') or '(none)'}",
        "(That summary is often a human placeholder in Grafana — do NOT treat it as technical root cause unless "
        "the Loki lines below support it.)",
        "",
        "Redacted Loki log sample (same time window as the alert):",
        "----",
        log_blob or "(no log lines in enrichment — infer only from alert metadata, or state that logs are missing.)",
        "----",
        "",
        "Pre-scan of the excerpt against the Loki error regex (case-insensitive: error, exception, fatal, "
        "panic, warn — same regex used for the error-focused Loki pass):",
        f"- error_signals_present: {'yes' if has_error_signals else 'NO'}",
        "",
    ]
    if not has_error_signals:
        lines.extend(
            [
                "CRITICAL — error_signals_present is NO:",
                "- The excerpt contains no error / warn / exception keywords. Do NOT name specific exception "
                "classes (UnauthorizedException, AuthGuard, HttpExceptionFilter, AxiosError, MongooseError, etc.) "
                "or fabricate failure modes from past Nest / Java / Axios patterns.",
                "- *Likely cause* must say something like: 'No error-level lines visible in the Loki window queried "
                "for this alert.'",
                "- *Evidence* should describe what IS in the excerpt (request paths, socket events, INFO lines) and "
                "explicitly note the absence of error keywords.",
                "- *Next steps* should be diagnostic, not service-fix: verify alert startsAt against Loki index "
                "timestamps (clock skew is a common cause), widen LOKI_ENRICH_WINDOW_MINUTES, re-pull the window "
                "manually in Grafana Explore, or check whether the Grafana alert rule's expression / threshold is "
                "still firing on stale data.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "CRITICAL — error_signals_present is YES (error/warn text exists): still no hallucinations.",
                "- Do NOT name NestJS, AuthGuard, HttpExceptionFilter, UnauthorizedException, Mongoose, Axios, OR "
                "any framework unless that EXACT word appears in the log excerpt. Stack frames showing "
                "`express/lib`, `pino-http`, `node_modules/express`, or `/app/src/server.js` mean Node.js + Express "
                "(or similar) — NOT Nest. Say so plainly; never substitute AuthGuard.",
                "- If JSON fields include `\"simulated\": true` or msg contains 'simulated error path' or path contains "
                "`/api/simulate-error`, this is an **intentional test/demo** route. Under *Likely cause*, write **two "
                "sentences of prose only** — **zero** lines starting with `-`, `•`, or `*` before *Evidence*, **no** "
                "chains like `- msg … - req.url …`, **no** quoting this prompt's classification criteria. "
                "**Sentence one must begin with (or immediately quote) the logged err.message** from the ERROR "
                "record — not with 'The failing route was …'. Sentence two = short clarification that `/api/simulate-error` "
                "is an intentional demo handler returning 500 by design (not customer impact / not a mystery outage).",
                "- For simulated routes, *Next steps* must be **positive** operator actions only (e.g. exclude "
                "`/api/simulate-error` from the alert query, mute/narrow the rule, run scheduled tests outside paging). "
                "**Avoid** vague bullets like 'investigate why the route is accessed', **'investigate … endpoint to "
                "understand why it's failing'**, 'misconfiguration', "
                "'production-ready fix', or 'mute to only production-ready endpoints' — the handler **fails on "
                "purpose** for demos; there is nothing to debug in application code for that scenario.",
                "- For simulated routes, **never** tell on-call to open Grafana to find **annotation_summary**, "
                "**dashboard annotations**, or 'additional context' from the Grafana UI — **forbidden** (same as "
                "section B). Tune Loki/alert queries or rule filters instead.",
                "**Never** describe the incident using Grafana jargon **'test enrichment'** in *Likely cause* or "
                "*Evidence* unless that exact substring appears in a **log line** — it is not a substitute for "
                "`err.message`.",
                "**Never** paste negative instruction lines from this prompt as bullets.",
                "- HTTP 500 in res.statusCode is a server-side response; do NOT rewrite it as 401 Unauthorized or "
                "auth failure. Only cite 401 if the substring 401 or Unauthorized appears in the log text.",
                "- Pino-http often emits TWO ERROR lines for one failed request: (1) application err with message + "
                "stack, (2) 'failed with status code 500' from the logger. Prefer (1) for root cause; (2) is "
                "instrumentation noise — **do not** dedicate a separate *Evidence* bullet to (2); at most one short "
                "clause that (2) is logger noise, **after** quoting (1)'s err.message.",
                "- Do NOT cite generic 'upstream database', 'Mongo', 'Postgres', 'Redis', or 'payment processor outage' "
                "unless those words or unmistakable DB/driver errors appear in the excerpt. Simulated timeout **text** "
                "is not evidence of a real DB incident.",
                "",
            ]
        )
    if operator_instructions:
        lines.extend(
            [
                "Operator-supplied notes (may include pasted lines from Grafana Explore or another source — "
                "timestamps may differ from the frozen Loki window stored with this alert):",
                operator_instructions.strip(),
                "",
                "Reconciliation: Combine the Loki sample above with these notes. If an ERROR, timeout, or stack "
                "trace appears only in the operator notes and not in the Loki block, say so explicitly under "
                "*Evidence* and still analyze it as relevant signal (do not dismiss it). Prefer concrete "
                "symptoms (timeouts, connection refused, DB errors) over vague Grafana dashboard placeholder "
                "phrases unless the logs clearly indicate a test. (Do not echo placeholder vocabulary as if it were "
                "log-backed evidence.)",
                "",
            ]
        )
    lines.extend(
        [
            "Analysis discipline (required — think like an on-call engineer, not a generic chatbot):",
            "",
            "(A) Path attribution — never mix 200 paths with auth failures.",
            "- If a url/path appears with statusCode 200 ('Request completed', 'Incoming request'), do NOT claim 401 / "
            "Unauthorized / auth failure for that path. Listing 200-only urls under auth failures is a false RCA.",
            "- Do not attach HttpExceptionFilter / AuthGuard WARN stacks to a specific route name unless path: or url: "
            "appears in the same record or adjacent lines for that failure. If absent, write 'failing route not shown "
            "next to the stack in this sample' and, in *Next steps*, suggest identifying it in Grafana (narrow time, "
            "filter by service, or log path: on the exception). Do NOT write 'repeated failures for a specific route' "
            "or 'focus on the specific route(s)' without path evidence.",
            "- Only call something 'the same route' when the same path/url string appears for both failure and success.",
            "",
            "(A2) Same-request attribution — windows often mix unrelated lines.",
            "- One excerpt may include WARN from `/api/orders` or `/api/stderr-sample` and ERROR from `/api/simulate-error`. "
            "**Never** describe the failing ERROR using `req.url` / `url` / `path` taken from a **different** log record. "
            "Pair `err`, `simulated`, and `res.statusCode` with the `req` block that shares the **same** `req.id` (or the "
            "same JSON line).",
            "- Do **not** invent URL paths from words inside `msg`, stderr output, or stack text (e.g. the phrase "
            "'manual stderr sample' is not `req.url` — only cite paths from structured fields like `req.url` or `url`).",
            "",
            "(B) Evidence handling.",
            "- Do NOT name specific exception classes, framework components, or error types "
            "(UnauthorizedException, AuthGuard, HttpExceptionFilter, AxiosError, MongoNetworkError, etc.) unless "
            "that EXACT string appears in the log excerpt above. If you would write 'AuthGuard' or "
            "'UnauthorizedException', search the excerpt for it first; if absent, omit the term entirely. This "
            "stops pattern-matching against past incidents that aren't in this window.",
            "- Grafana annotation_summary is often human dashboard placeholder text. Do NOT mention "
            "**annotation_summary** or 'Grafana annotation' anywhere in the RCA (including *Evidence*) — not even to "
            "dismiss it. *Likely cause* and *Evidence* must come from Loki log lines only. Do NOT recommend in "
            "*Next steps* anything about investigating dashboard annotation copy. On-call acts on services and logs, "
            "not annotations.",
            "- Cite concrete log fields when present: path/url, statusCode, method, req.id, correlationId, ip, "
            "userAgent. **Never invent** a correlationId (or any ID) — if the excerpt shows `req.id` but not "
            "`correlationId`, cite `req.id` or say correlation id not present; do not substitute placeholder hex strings.",
            "- Never claim correlationIds 'match' or 'align' across records unless the excerpt shows the SAME id string "
            "on both lines being compared; otherwise drop correlationId linkage.",
            "- Do **not** claim `req.id` is 'shared between WARN and ERROR' unless those lines show the **exact same** "
            "id string — different routes usually have **different** ids; unrelated WARN + ERROR lines are not one "
            "request lifecycle.",
            "- Do **not** spend *Evidence* bullets debating whether unrelated WARN and ERROR lines share `req.id` — cite "
            "`req.id` **only** from the ERROR JSON line(s) for the failing request; ignore ids from other routes unless "
            "they are provably the same string.",
            "- Do **not** invent hybrid log lines (e.g. 'Incoming request' / Request completed 200 **on the same line** "
            "as an ERROR JSON record). JSON logs are one record per line; cite each record accurately.",
            "- A SINGLE correlationId appearing in multiple log lines is the NORMAL request lifecycle "
            "(Incoming request → middleware → Request completed). Do NOT cite that as evidence of repeated "
            "failures or a recurring issue. Repeats need DISTINCT correlationIds with the same failure stack.",
            "- Avoid quantitative claims ('high volume of 401', counts, percentages) unless the excerpt contains those "
            "statusCodes or numbers.",
            "- **Never** use *Evidence* bullets that only negate absent systems ('No upstream database mentioned', 'No "
            "external outage in logs') — those add no signal for a simulated route.",
            "- IPs like ::ffff:172.16.x.x are usually load-balancer / internal hops; call them 'proxy/LB-facing "
            "address in logs', not 'the client', unless logs say so.",
            "",
            "(C) Scope and operator instructions.",
            "- If the window contains MORE THAN ONE distinct issue (auth WARN vs 4xx/5xx upstream / DB), treat them as "
            "SEPARATE threads: primary cause vs optional *Additional context* — never merge unrelated stacks.",
            "- For 401 / missing Authorization on protected routes: acknowledge anonymous / stale clients are EXPECTED; "
            "recommend Grafana rule tuning or rate thresholds when the pattern looks like noise without customer impact.",
            "- For downstream failures (external HTTP 500, wallet/on-chain service, DB), point at that dependency "
            "only when logs literally mention that system or show its client errors — do not invent outages.",
            "- When operator notes anchor a time ('around 5:29 PM'), narrate only log lines at or near that timestamp; "
            "if stacks there lack path/url, say so — do not infer failures on routes that only appear elsewhere with 200.",
            "",
            "Deliver a concise RCA for on-call engineers. Max ~350 words.",
            "",
            "Formatting (Slack mrkdwn — # headings DO NOT render):",
            "- Three section titles, one per line: write *Likely cause*  *Evidence*  *Next steps* "
            "with EXACTLY ONE asterisk on each side. NEVER **two** or ***three*** asterisks — Slack "
            "renders triple-asterisks as literal text.",
            "- One blank line after each title before paragraphs or lists. Do NOT continue a sentence on "
            "the same line as the title.",
            "- *Evidence* and *Next steps* use '- ' bullets, one per line; never glue a bullet to the title line.",
            "- Do **not** nest extra '- ' bullets inside one bullet (no 'topic: - a - b'). Use **one** simple fact per "
            "top-level bullet; if describing pino's two ERROR lines, say it in **one** sentence in **one** bullet.",
            "- Do **not** format *Evidence* as chained pseudo-fields on one line (`req.id: … - simulated: … - "
            "res.statusCode: …`). Use normal sentences or **one** fact per `- ` bullet.",
            "- Do **not** use placeholder ids like `XXXXXXXX`; quote `req.id` exactly from the excerpt or write "
            "'req.id present (redacted sample)'.",
            "- Add *Additional context* (same single-asterisk style) ONLY when a **second** issue is **explicitly visible** "
            "in the excerpt (different failing path/stack than the primary). NEVER use *Additional context* to introduce "
            "AuthGuard, authentication failures, database incidents, or generic upstream outages unless those words "
            "appear literally in the log sample. If the window is mostly simulated/test routes (`simulated`, "
            "`simulate-error`, intentional demo errors), **omit** *Additional context* entirely — do not invent a "
            "parallel auth or DB story.",
            "- If there is **no** genuine second issue, **omit** *Additional context* completely — **never** output "
            "placeholder lines such as 'No additional issues', 'none visible', or 'no other failures'. Those waste "
            "Slack space and read like filler.",
            "- Do not use ### / ## / **bold** / ***bold-italic*** pseudo-headings.",
            "- The FIRST visible output text must be *Likely cause* — forbid preamble phrases ('Here is', "
            "'Below is', 'Concise RCA', 'for on-call engineers').",
            "- Under *Evidence* and *Next steps*, use '- ' bullets only — forbid numbered lists ('1.', '2.', '3.').",
            "- Do not hedge with 'unclear if database or external service outage' when logs show only a "
            "simulated/demo message — attribute the scenario to the test handler, not a generic outage class.",
            "- *Additional context* must not contain generic filler ('if you find another path', 'investigate "
            "further', 'no additional issues') — either cite a second concrete log-backed issue or omit the section.",
            "",
            "Slack readability — your answer must NOT look like a copy of this prompt:",
            "- Do not output classification checklists ('- simulated: true - path contains …'). Write plain sentences.",
            "- *Likely cause* is **never** a bullet list — bullets `- ` belong under *Evidence* and *Next steps* only.",
            "- Between *Likely cause* and *Evidence*, every line must be **plain prose** (no `- ` at line start). If you "
            "catch yourself listing msg/url/status fields with dashes, rewrite as sentences.",
            "- *Next steps* bullets describe what on-call **should do**, not meta-rules ('do not escalate').",
            "- Do not fill *Evidence* with negations about what is NOT in logs unless one short bullet helps scope "
            "(prefer affirmative quotes from the excerpt).",
        ]
    )
    if has_error_signals and _excerpt_looks_simulated_demo_route(log_blob):
        lines.extend(_final_simulated_route_reminder_lines())
    return "\n".join(lines)


async def generate_rca_ollama(
    _payload: Dict[str, Any],
    summary: Dict[str, Any],
    enrichment: Dict[str, Any],
    *,
    model_override: str | None = None,
    user_prompt: str | None = None,
) -> Dict[str, Any]:
    """
    Produce RCA text for Slack/storage: either a **deterministic template** for ``/api/simulate-error`` demo logs
    (when ``OLLAMA_RCA_SIMULATED_TEMPLATE_OVERRIDE`` is true and there are no operator instructions), or one call to
    Ollama ``/api/chat``.

    Returns keys: ``text``, ``model``, optional ``error``, ``skipped_reason``, or ``simulated_demo_template`` when the
    fixed demo-route RCA was used (no inference).

    ``model_override`` selects the model tag when calling Ollama. ``user_prompt`` appends operator instructions; any
    non-empty text **disables** the simulated template so rerun overrides still hit the LLM.
    ``_payload`` is retained for API compatibility (alert JSON); prompt assembly uses ``summary`` + Loki enrichment only.
    """
    if not settings.ollama_rca_enabled:
        return {"skipped_reason": "ollama_rca_disabled"}

    model_use = (model_override.strip() if isinstance(model_override, str) and model_override.strip() else None) or settings.ollama_model

    lines = enrichment_log_lines(enrichment)
    raw_blob = "\n".join(lines)
    max_c = settings.ollama_rca_max_log_chars
    if len(raw_blob) > max_c:
        raw_blob = raw_blob[:max_c] + "\n... [truncated for model context]"

    log_blob = _redact_for_llm(raw_blob)
    extra = None
    if isinstance(user_prompt, str) and user_prompt.strip():
        max_u = settings.ollama_rca_user_prompt_max_chars
        extra = user_prompt.strip()
        if len(extra) > max_u:
            extra = extra[:max_u] + "\n... [truncated for model context]"

    if (
        settings.ollama_rca_simulated_template_override
        and not extra
        and _excerpt_has_error_signals(log_blob)
        and _excerpt_looks_simulated_demo_route(log_blob)
    ):
        msg = _extract_simulated_err_message(log_blob) or (
            "Simulated downstream failure (demo handler — could not parse err.message from excerpt)."
        )
        text = _render_simulated_demo_rca(msg)
        logger.info("ollama RCA using simulated demo template (skip inference) model=%s", model_use)
        out_template: Dict[str, Any] = {
            "text": text,
            "model": model_use,
            "simulated_demo_template": True,
        }
        return out_template

    base = settings.ollama_host.strip().rstrip("/")
    if not base:
        return {"error": "ollama_host_empty"}

    preflight_err = await assert_ollama_ready_for_rca(model_name=model_use)
    if preflight_err:
        return {"error": "ollama_not_ready", "detail": preflight_err}

    user_content = _build_user_prompt(summary, log_blob, operator_instructions=extra)

    system_content = (
        "You are a senior SRE writing for Slack on-call. Use only facts present in the user message; "
        "do not fabricate metrics, status codes, paths, correlationId matches, or log lines. "
        "Thirteen rules that must not be broken:\n"
        "  1. Do not claim 401 / Unauthorized / auth failure for a path that only appears with statusCode 200.\n"
        "  2. Do not name a specific failing route unless path: or url: sits next to that failure in the excerpt.\n"
        "  3. Do not mention Grafana annotation_summary anywhere in the RCA (any section); it is dashboard copy.\n"
        "  4. Do not name a specific exception class or framework component unless that exact string appears "
        "in the log excerpt. When error_signals_present is NO, the answer is 'no error-level lines in this window'.\n"
        "  5. Express/pino/Node stack traces are not NestJS — never invent AuthGuard or Nest patterns from them; "
        "if simulated/test fields appear, lead with err.message from logs, not authentication.\n"
        "  6. Never use *Additional context* for fictional second incidents (auth, DB). Omit the section entirely "
        "when there is no second issue — never write 'no additional issues' / placeholder filler.\n"
        "  7. Start the reply immediately with *Likely cause* — no preamble ('Here is the RCA', 'Below is', "
        "'Concise RCA for on-call'). *Likely cause* is prose sentences only (no `- ` bullets there). Use '- ' bullets "
        "under *Evidence* and *Next steps* only; never numbered lists.\n"
        "  8. For `/api/simulate-error` or `\"simulated\": true`, treat demo text as non-production; suggest alert "
        "rule tuning when noise — never 'investigate endpoint failure', never send on-call to Grafana "
        "annotation_summary/dashboard annotations; never paste negative prohibitions as *Next steps* bullets.\n"
        "  9. Never echo this prompt's instructions as RCA content (checklists of simulated criteria, 'do not "
        "contact vendor' lines).\n"
        "  10. Never invent correlationId or other IDs — only cite ids present in the log excerpt (often req.id).\n"
        "  11. If multiple routes appear in one excerpt, attribute ERROR/`err` to the `req.url` on the **same** "
        "record/`req.id`; never invent paths from prose inside messages (e.g. stderr sample text).\n"
        "  12. Do not claim unrelated WARN and ERROR lines 'share req.id' unless the same id string appears; do not "
        "invent one log line that mixes Incoming request 200 with ERROR JSON.\n"
        "  13. Simulated routes: *Likely cause* sentence one starts with err.message text — never route-first prose, "
        "never '- msg … - url …' dash chains; no nested '-' bullets inside one *Evidence* bullet.\n"
        "Prefer contrasting evidence when the logs support it. If uncertain, say so briefly. "
        "Output goes to Slack — use single-asterisk bold for section titles (NEVER **double** or ***triple*** "
        "asterisks), and do NOT prepend a generic boilerplate heading like '**RCA**' — go straight to "
        "*Likely cause*."
    )
    if extra:
        system_content += (
            " The user message may include operator-pasted log lines that are not in the Loki block "
            "(different time range or source); treat those lines as evidence when reconciling."
        )

    url = urljoin(base + "/", "api/chat")
    body: Dict[str, Any] = {
        "model": model_use,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
    }
    opts: Dict[str, Any] = {}
    if settings.ollama_rca_temperature is not None:
        opts["temperature"] = settings.ollama_rca_temperature
    if settings.ollama_rca_top_p is not None:
        opts["top_p"] = settings.ollama_rca_top_p
    if opts:
        body["options"] = opts
        logger.info("ollama RCA using request options=%s model=%s", opts, model_use)

    # Separate connect vs read: slow CPU inference only extends read; avoids ambiguous single-timeout behavior.
    read_s = max(30.0, float(settings.ollama_timeout_seconds))
    timeout = httpx.Timeout(connect=30.0, read=read_s, write=60.0, pool=30.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("ollama RCA HTTP error status=%s", exc.response.status_code if exc.response else None)
        return {"error": "http_error", "detail": str(exc)}
    except Exception as exc:
        logger.exception("ollama RCA request failed")
        return {"error": type(exc).__name__, "detail": str(exc)}

    if not isinstance(data, dict):
        return {"error": "invalid_response"}

    message = data.get("message")
    text = ""
    if isinstance(message, dict):
        text = str(message.get("content") or "").strip()

    if not text:
        return {"error": "empty_completion", "raw": json.dumps(data)[:500]}

    out: Dict[str, Any] = {
        "text": text,
        "model": model_use,
    }
    if opts:
        out["request_options"] = opts
    return out
