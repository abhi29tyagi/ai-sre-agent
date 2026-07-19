"""Slack Events API: thread replies trigger RCA rerun (optional bot confirmation)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.services.rca_rerun import RcaRerunFailure, execute_rca_rerun
from app.services.slack_dedupe import slack_event_dedupe
from app.services.slack_notify import format_rca_preview_for_slack
from app.services.slack_verify import verify_slack_signature

logger = logging.getLogger(__name__)
router = APIRouter()

_SLACK_API_BASE = "https://slack.com/api"

# Short Ollama tags without digits (single-token model override after event id).
_SHORT_OLLAMA_TAGS = frozenset(
    {
        "mistral",
        "mixtral",
        "gemma",
        "gemma2",
        "phi3",
        "phi4",
        "codellama",
        "llama3",
        "qwen",
        "qwen2",
    }
)


def _looks_like_single_token_model(tag: str) -> bool:
    t = tag.strip()
    if not t or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:+.\-]*", t):
        return False
    tl = t.lower()
    if tl in _SHORT_OLLAMA_TAGS:
        return True
    if any(ch.isdigit() for ch in t):
        return True
    if "." in t:
        return True
    return False


def _parse_rca_command(text: str) -> Optional[tuple[int, Optional[str], Optional[str]]]:
    """
    Parse ``rca|rerun <id>`` with optional model and/or operator instructions.

    Returns ``(event_id, model_override, user_prompt)``.

    - ``rerun 32`` — defaults only
    - ``rerun 32 llama3.2`` — model override (token looks like a model tag)
    - ``rerun 32 -- Focus on Mongo timeouts`` — custom prompt
    - ``rerun 32 llama3.2 -- Focus on replication lag`` — model + prompt
    - ``rerun 32 Please assess blast radius`` — multi-word tail is the prompt (no ``--``)
    """
    if not text or not isinstance(text, str):
        return None
    t = re.sub(r"<@[^>]+>\s*", "", text).strip()
    m = re.match(r"(?is)^\s*(?:rca|rerun)(?:\s+event)?\s*#?\s*(\d+)\s*(.*)$", t)
    if not m:
        return None
    event_id = int(m.group(1))
    rest = (m.group(2) or "").strip()
    if not rest:
        return (event_id, None, None)

    if "--" in rest:
        before, after = rest.split("--", 1)
        before_s, after_s = before.strip(), after.strip()
        model: Optional[str] = None
        if before_s:
            btoks = before_s.split()
            if len(btoks) == 1 and _looks_like_single_token_model(btoks[0]):
                model = btoks[0]
            else:
                after_s = f"{before_s} — {after_s}".strip() if after_s else before_s
        return (event_id, model, after_s or None)

    tokens = rest.split()
    if len(tokens) == 1 and _looks_like_single_token_model(tokens[0]):
        return (event_id, tokens[0], None)

    return (event_id, None, rest)


async def _slack_rca_rerun_job(
    channel: str,
    thread_ts: str,
    event_id: int,
    model_override: Optional[str],
    user_prompt: Optional[str],
) -> None:
    """Runs after Slack gets HTTP 200 — Ollama can take minutes; Events API expects ~3s ACK."""
    try:
        result = await execute_rca_rerun(
            event_id,
            model=model_override,
            user_prompt=user_prompt,
            notify_slack=None,
        )
    except RcaRerunFailure as err:
        await _post_thread_ack(
            channel,
            thread_ts,
            f":x: RCA rerun failed — `{err.detail}` (HTTP {err.status_code}).",
        )
        return
    except Exception:
        logger.exception("slack RCA background job failed event_id=%s", event_id)
        await _post_thread_ack(
            channel,
            thread_ts,
            f":x: RCA rerun crashed for event `{event_id}` — check agent logs.",
        )
        return

    rca = result.get("rca_ollama") if isinstance(result.get("rca_ollama"), dict) else {}
    err_txt = rca.get("error")
    if err_txt:
        await _post_thread_ack(
            channel,
            thread_ts,
            f":warning: Ollama returned an error for event `{event_id}`: `{err_txt}`",
        )
    else:
        preview = format_rca_preview_for_slack(str(rca.get("text") or ""), preview_max=1800).strip()
        if not preview:
            preview = "_(empty RCA text)_"
        extra = ""
        if isinstance(result.get("rca_rerun_user_prompt"), str) and result["rca_rerun_user_prompt"].strip():
            extra = "\n_Operator instructions were applied (stored on the event)._"
        ack = (
            f":white_check_mark: *RCA rerun stored* for event `{event_id}` "
            f"(model `{rca.get('model')}`).{extra}\n\n"
            f"{preview}\n\n"
            f"────────────────\n"
            f"_Optional full digest to channel: `NOTIFY_SLACK_ON_RCA_RERUN` (env) or `\"notify_slack\": true` on rerun API · "
            f"`GET /events/recent`_"
        )
        await _post_thread_ack(channel, thread_ts, ack, mrkdwn=True)


def _slack_channel_suffix(channel_id: str) -> str:
    """Last segment of channel ID for logs (avoid logging full IDs)."""
    if not channel_id or len(channel_id) < 5:
        return channel_id or "?"
    return f"…{channel_id[-6:]}"


async def _post_thread_ack(channel: str, thread_ts: str, text: str, *, mrkdwn: bool = False) -> None:
    token = settings.slack_bot_token.strip()
    if not token:
        return
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    body: dict[str, Any] = {"channel": channel, "thread_ts": thread_ts, "text": text}
    if mrkdwn:
        # Render ### headings and bullets (not inside ```); Block Kit mrkdwn section max ~3000 chars.
        md = text if len(text) <= 2900 else text[:2897] + "…"
        body["blocks"] = [{"type": "section", "text": {"type": "mrkdwn", "text": md}}]

    async def _chat_post(client: httpx.AsyncClient) -> dict[str, object]:
        resp = await client.post(f"{_SLACK_API_BASE}/chat.postMessage", headers=headers, json=body)
        return resp.json()  # type: ignore[no-any-return]

    async def _conversations_join(client: httpx.AsyncClient) -> dict[str, object]:
        resp = await client.post(f"{_SLACK_API_BASE}/conversations.join", headers=headers, json={"channel": channel})
        return resp.json()  # type: ignore[no-any-return]

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            data = await _chat_post(client)
            if data.get("ok"):
                return

            err = data.get("error")
            if err == "missing_scope":
                logger.warning(
                    "slack chat.postMessage missing_scope: Slack requires bot scope %s for thread replies. "
                    "OAuth & Permissions → Bot Token Scopes → add it (often chat:write:bot), then reinstall the app. "
                    "Provided scopes were: %s",
                    data.get("needed"),
                    data.get("provided"),
                )
                return

            if err in ("not_in_channel", "channel_not_found"):
                # DM channel IDs start with D — join does not apply; need the bot in the DM / conversations.open.
                if channel.startswith("D"):
                    logger.warning(
                        "slack chat.postMessage %s channel=%s — DMs require the bot to be in the conversation",
                        err,
                        _slack_channel_suffix(channel),
                    )
                    return

                join_data = await _conversations_join(client)
                if join_data.get("ok"):
                    data = await _chat_post(client)
                    if data.get("ok"):
                        logger.info(
                            "slack thread ack ok after conversations.join channel=%s",
                            _slack_channel_suffix(channel),
                        )
                        return

                logger.warning(
                    "slack chat.postMessage failed (%s) join=%s post_retry=%s channel=%s — "
                    "invite @bot to private channels; public channels need Bot scope channels:join or /invite @bot",
                    err,
                    join_data,
                    data,
                    _slack_channel_suffix(channel),
                )
                return

            logger.warning("slack chat.postMessage failed: %s", data)
    except Exception:
        logger.exception("slack thread ack failed")


@router.post("/hooks/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    """
    Slack Events API request URL. Configure in the Slack app (Event Subscriptions).

    Requires ``SLACK_SIGNING_SECRET``. Optional ``SLACK_BOT_TOKEN`` — needs Bot scopes **chat:write:bot**
    (and history scopes) so ``chat.postMessage`` can post success/error notes in the thread.

    In a **thread under the alert digest**, reply with ``rca <event_id>`` or ``rerun <event_id>``
    (synonyms — both **re-run Ollama** on the stored event snapshot). Event id matches ``event `N``
    in the digest. Optional model: ``rca 18 llama3.1``. Optional operator instructions:
    ``rerun 18 -- Focus on database connectivity`` or ``rerun 18 llama3.2 -- …``. Whether to post a full webhook digest after rerun is controlled by ``NOTIFY_SLACK_ON_RCA_RERUN`` (see README).
    """
    raw = await request.body()
    secret = settings.slack_signing_secret.strip()
    if not secret:
        raise HTTPException(status_code=503, detail="Slack Events not configured (set SLACK_SIGNING_SECRET)")

    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    if not verify_slack_signature(signing_secret=secret, timestamp=ts, body=raw, signature=sig):
        raise HTTPException(status_code=401, detail="invalid Slack signature")

    # Slack retries when our 3s ACK is missed; treat any retry as a duplicate to avoid
    # double-running long Ollama RCAs. Always 200 to satisfy Slack's retry policy.
    if request.headers.get("X-Slack-Retry-Num"):
        logger.info(
            "slack events: retry delivery dropped reason=%s",
            request.headers.get("X-Slack-Retry-Reason") or "unknown",
        )
        return JSONResponse(content={"ok": True, "skipped": "retry"})

    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc

    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge")
        if isinstance(challenge, str):
            return JSONResponse(content={"challenge": challenge})
        raise HTTPException(status_code=400, detail="missing challenge")

    if payload.get("type") != "event_callback":
        return JSONResponse(content={"ok": True})

    slack_event_id = payload.get("event_id")
    if isinstance(slack_event_id, str) and not await slack_event_dedupe.claim(slack_event_id):
        logger.info("slack events: duplicate event_id dropped %s", slack_event_id)
        return JSONResponse(content={"ok": True, "skipped": "duplicate"})

    ev = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    if not ev:
        return JSONResponse(content={"ok": True})

    if ev.get("type") != "message":
        return JSONResponse(content={"ok": True})

    raw_text = ev.get("text") or ""
    text = raw_text if isinstance(raw_text, str) else str(raw_text)
    looks_like_command = bool(re.search(r"(?i)\b(?:rca|rerun)\b", text))

    if ev.get("bot_id"):
        if looks_like_command:
            logger.info("slack events: skipped command-like message from bot user")
        return JSONResponse(content={"ok": True})

    subtype = ev.get("subtype")
    if subtype in ("message_changed", "message_deleted", "bot_message"):
        if looks_like_command:
            logger.info("slack events: skipped command-like message subtype=%s", subtype)
        return JSONResponse(content={"ok": True})

    raw_ch = ev.get("channel") or ev.get("channel_id")
    channel = raw_ch.strip() if isinstance(raw_ch, str) else ""
    ts_msg = ev.get("ts")
    thread_ts = ev.get("thread_ts")

    # Require a reply inside a thread (not the root digest message).
    if not channel or not ts_msg:
        if looks_like_command:
            logger.info(
                "slack events: skipped command-like message missing channel/ts channel_present=%s ts_present=%s",
                bool(channel),
                bool(ts_msg),
            )
        return JSONResponse(content={"ok": True})
    if not thread_ts or thread_ts == ts_msg:
        if looks_like_command:
            logger.info(
                "slack events: skipped command-like message not a thread reply channel=%s thread_ts=%s ts=%s",
                _slack_channel_suffix(channel),
                bool(thread_ts),
                bool(ts_msg),
            )
        return JSONResponse(content={"ok": True})

    parsed = _parse_rca_command(text)
    if not parsed:
        if looks_like_command:
            logger.info(
                "slack events: command parse failed text=%r channel=%s",
                text[:200],
                _slack_channel_suffix(channel),
            )
        return JSONResponse(content={"ok": True})

    event_id, model_override, user_prompt = parsed
    background_tasks.add_task(
        _slack_rca_rerun_job,
        channel,
        thread_ts,
        event_id,
        model_override,
        user_prompt,
    )
    logger.info(
        "slack events: scheduled RCA rerun event_id=%s custom_prompt=%s (background)",
        event_id,
        bool(user_prompt and user_prompt.strip()),
    )
    return JSONResponse(content={"ok": True})
