"""Slack Incoming Webhook notifications."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import settings
from app.services.suggestions import (
    _collapse_section_title_asterisks,
    _ensure_blank_line_before_rca_sections,
    build_operator_suggestions,
    checklist_skip_explainer,
    enrichment_log_lines,
    format_log_snippets,
    prioritize_log_lines_for_snippets,
    redact_for_slack,
    should_skip_operator_checklist,
)

logger = logging.getLogger(__name__)

# Slack Block Kit: mrkdwn max ~3000 chars per section text; header plain_text max 150.
_MRKW_MAX = 2900
_HEADER_MAX = 150


def _chunk_text(text: str, size: int) -> List[str]:
    if not text:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]


def _spacing_for_slack_mrkdwn(text: str) -> str:
    """
    Normalize Ollama RCA markdown for Slack mrkdwn.

    Fixes glued headings/bullets, then converts ATX ``###`` lines to Slack **bold**
    (``*Section title*``) because Slack does not render ``#`` markdown as headings.
    """
    t = text.strip()
    if not t:
        return t

    # Collapse ***Title*** / **Title** for the canonical RCA section titles to *Title* + blank line.
    # Slack mrkdwn does not render double / triple asterisks as bold; this prevents literal "***" output.
    t = _collapse_section_title_asterisks(t)
    t = _ensure_blank_line_before_rca_sections(t)

    # ### Likely cause / Evidence / Next steps — paragraph glued on same line as heading (common model glitch).
    t = re.sub(r"(?:^|\n)(###\s+Likely cause)\s+(?=[A-Z])", r"\1\n\n", t, flags=re.IGNORECASE)
    t = re.sub(r"(?:^|\n)(###\s+Evidence)\s+(?=[A-Za-z\-*])", r"\1\n\n", t, flags=re.IGNORECASE)
    t = re.sub(r"(?:^|\n)(###\s+Next steps)\s+(?=[A-Za-z\-*])", r"\1\n\n", t, flags=re.IGNORECASE)
    t = re.sub(r"(?:^|\n)(####\s+Additional context:?)\s+(?=\S)", r"\1\n\n", t, flags=re.IGNORECASE)

    # **Section title** Same-line paragraph → blank line after bold title (Slack renders poorly when glued).
    t = re.sub(r"(?:^|\n)(\*\*[^*\n]+\*\*)\s+(?=\S)", r"\1\n\n", t)

    # ### Heading * bullet — model often emits "* bullet" glued to the heading line.
    t = re.sub(r"(#{1,6}\s[^\n]+?)\s+\*\s+", r"\1\n\n* ", t)

    # ### Heading - dash bullet — " ### Evidence - The …" on one line
    t = re.sub(r"(#{1,6}\s[^\n]+?)\s+-\s+(?=\S)", r"\1\n\n- ", t)

    # Several "* Item" bullets on one line — split before "* Capitalized…"
    t = re.sub(r"(?<=[a-z0-9.\)\"])\s+\*\s+(?=[A-Z][a-z])", r"\n* ", t)

    # Several "- Item" bullets on one line — "…summary\".- The next …"
    t = re.sub(r"(?<=[a-z0-9.\"\)])\s+-\s+(?=[A-Z])", r"\n- ", t)

    # #### Subheading … 1. First step — numbered list glued after heading text.
    t = re.sub(r"(#{4,6}\s.*?)\s+(\d+\.\s+[A-Za-z])", r"\1\n\n\2", t)

    # "…sentence end. 2. Next step" on one line — split numbered continuation after a period.
    t = re.sub(r"\.(\s+)(\d+\.\s+[A-Z])", r".\n\n\2", t)

    # Two markdown headings on one line: "### A ### B"
    t = re.sub(r"(#{1,6}\s[^\n]+?)(\s+)(#{1,6}\s)", r"\1\n\n\3", t)

    # ### glued after prose on same line — insert breaks before ATX heading (never split existing ###).
    t = re.sub(r"(?<=[^\n#])(#{1,6}\s+\S)", r"\n\n\1", t)

    # Adjacent bold segments: foo** **bar → separate paragraphs
    t = re.sub(r"\*\*\s+\*\*", "**\n\n**", t)

    # Collapse excessive blank lines from chained fixes
    t = re.sub(r"\n{3,}", "\n\n", t)

    # Ensure a blank line before major ### section breaks (Slack bold titles read better).
    t = re.sub(
        r"(?<=\S)\n(###\s+(?:Likely cause|Evidence|Next steps)\b)",
        r"\n\n\1",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(r"(?<=\S)\n(####\s+Additional context)", r"\n\n\1", t, flags=re.IGNORECASE)

    # Slack mrkdwn has no real ATX headings — turn ### lines into bold titles (*Like this*).
    t = _atx_headings_to_slack_bold(t)
    return t.strip()


def _atx_headings_to_slack_bold(text: str) -> str:
    """Replace whole-line ATX headings (# … ###) with Slack mrkdwn bold (*Title*)."""
    out: List[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        m = re.match(r"^#{1,6}\s+(.+)$", stripped)
        if m:
            title = m.group(1).strip()
            if title:
                out.append(f"*{title}*")
                continue
        out.append(line)
    return "\n".join(out)


def _truncate_rca_preview(text: str, max_len: int = 1800) -> str:
    """Shorten RCA text for Slack thread previews — prefer paragraph / line / word boundaries."""
    t = text.strip()
    if len(t) <= max_len:
        return t
    cut = t[:max_len]
    para = cut.rfind("\n\n")
    if para > max_len // 3:
        return cut[:para].rstrip() + "\n\n…"
    nl = cut.rfind("\n")
    if nl > max_len // 3:
        return cut[:nl].rstrip() + "\n…"
    sp = cut.rfind(" ")
    if sp > max_len // 3:
        return cut[:sp].rstrip() + "…"
    return cut.rstrip() + "…"


def format_rca_preview_for_slack(rca_text: str, *, preview_max: int = 1800) -> str:
    """Redact + Slack-normalize + truncate — same shaping as digest RCA sections (for thread replies)."""
    if not rca_text or not isinstance(rca_text, str):
        return ""
    shaped = _spacing_for_slack_mrkdwn(redact_for_slack(rca_text.strip(), max_len=7500))
    return _truncate_rca_preview(shaped, preview_max)


def _mrkdwn_sections(label: str, body: str, max_body: int = _MRKW_MAX) -> List[Dict[str, Any]]:
    """One or more section blocks with a bold label on the first chunk."""
    body = body.strip()
    if not body:
        return []
    parts = _chunk_text(body, max_body)
    out: List[Dict[str, Any]] = []
    for i, chunk in enumerate(parts):
        prefix = f"*{label}*\n" if i == 0 else ""
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": f"{prefix}{chunk}"}})
    return out


def _plain_fallback(
    header_plain: str,
    ctx_line: str,
    rca_preview: str,
    bullets: str,
    snippets: str,
    thread_hint: str = "",
) -> str:
    """Multi-line fallback for mobile / notifications when blocks are flattened."""
    parts: List[str] = [
        header_plain,
        "",
        "────────",
        "",
        rca_preview.strip()[:2800],
        "",
        "────────",
        "",
        "*Operator checklist*",
        bullets.strip()[:1200],
    ]
    if snippets.strip():
        parts.extend(["", "────────", "", "*Log snippets*", snippets.strip()[:800]])
    parts.extend(["", "────────", "", ctx_line])
    if thread_hint.strip():
        parts.extend(["", thread_hint])
    text = "\n".join(parts)
    return text[:12000]


def _build_slack_blocks(
    summary: Dict[str, Any],
    enrichment: Optional[Dict[str, Any]],
    event_id: Optional[int],
) -> Tuple[List[Dict[str, Any]], str, str]:
    skip_checklist, skip_code = should_skip_operator_checklist(
        summary,
        enrichment,
        enabled=settings.slack_smart_skip_operator_checklist,
    )
    if skip_checklist:
        bullets = redact_for_slack(checklist_skip_explainer(skip_code), max_len=1200)
        if skip_code:
            logger.info(
                "slack operator checklist suppressed (low_signal) code=%s alertname=%s",
                skip_code,
                summary.get("alertname"),
            )
    else:
        suggestions = build_operator_suggestions(summary, enrichment)
        bullets = "\n".join(f"• {redact_for_slack(s, max_len=500)}" for s in suggestions)

    raw_lines = enrichment_log_lines(enrichment)
    lines = prioritize_log_lines_for_snippets(raw_lines)
    snippets = format_log_snippets(lines)

    env = summary.get("environment") or "unknown"
    alertname = summary.get("alertname") or "alert"
    status = summary.get("status") or "unknown"
    ev = f"`{event_id}`" if event_id is not None else "n/a"

    header_plain = f"[{env}] {alertname} — {status} — event {ev}"
    if len(header_plain) > _HEADER_MAX:
        header_plain = header_plain[: _HEADER_MAX - 1] + "…"

    ctx_line = (
        f"Agent default model `{settings.ollama_model}` · "
        f"RCA run may use another tag when you override via `/events/…/rca/rerun`."
    )
    thread_hint = ""
    if event_id is not None:
        thread_hint = (
            f"*In this thread:* reply with `rca {event_id}` or `rerun {event_id}` to re-run RCA "
            f"(model: `rca {event_id} llama3.2`; custom prompt: `rerun {event_id} -- your instructions`)."
        )

    ctx_elements: List[Dict[str, Any]] = [{"type": "mrkdwn", "text": ctx_line}]
    if thread_hint:
        ctx_elements.append({"type": "mrkdwn", "text": thread_hint})

    blocks: List[Dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header_plain, "emoji": True}},
        {"type": "divider"},
    ]

    rca_info = (enrichment or {}).get("rca_ollama") if isinstance(enrichment, dict) else None
    rca_info = rca_info if isinstance(rca_info, dict) else {}

    rca_text = rca_info.get("text")
    rca_model = rca_info.get("model") or settings.ollama_model

    rca_plain_for_fallback = ""

    if isinstance(rca_text, str) and rca_text.strip():
        label = f"RCA (Ollama — `{rca_model}`)"
        body = _spacing_for_slack_mrkdwn(redact_for_slack(rca_text.strip(), max_len=7500))
        rca_plain_for_fallback = f"*{label}*\n\n{body}"
        blocks.extend(_mrkdwn_sections(label, body))
    elif rca_info.get("error"):
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*RCA (Ollama): unavailable*\n`{rca_info.get('error')}`",
                },
            }
        )
        rca_plain_for_fallback = f"RCA unavailable: {rca_info.get('error')}"
    elif rca_info.get("skipped_reason"):
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*RCA (Ollama): skipped*\n{rca_info.get('skipped_reason')}",
                },
            }
        )
        rca_plain_for_fallback = f"RCA skipped: {rca_info.get('skipped_reason')}"

    blocks.append({"type": "divider"})
    blocks.extend(_mrkdwn_sections("Operator checklist (rules)", bullets))
    if snippets:
        blocks.append({"type": "divider"})
        blocks.extend(_mrkdwn_sections("Recent log snippets (redacted)", snippets))

    blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": ctx_elements})

    fallback = _plain_fallback(
        header_plain,
        ctx_line,
        rca_plain_for_fallback or "(no RCA text)",
        bullets,
        snippets,
        thread_hint=thread_hint,
    )

    return blocks[:49], fallback, skip_code


async def notify_grafana_alert(
    summary: Dict[str, Any],
    enrichment: Optional[Dict[str, Any]],
    event_id: Optional[int],
    *,
    slack_notify_enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Post heuristic checklist + optional Ollama RCA + snippets to Slack.
    No-op if SLACK_WEBHOOK_URL is unset or effective Slack notify is false.

    ``slack_notify_enabled``: when ``None``, uses ``settings.slack_notify_enabled``; Grafana webhooks can pass
    a per-request value from annotations / labels / query string (see README).
    """
    if not settings.slack_webhook_url:
        return {"sent": False, "reason": "no_webhook_url"}
    effective_notify = settings.slack_notify_enabled if slack_notify_enabled is None else slack_notify_enabled
    if not effective_notify:
        return {"sent": False, "reason": "slack_notify_disabled"}

    env = summary.get("environment") or "unknown"
    alertname = summary.get("alertname") or "alert"

    blocks, fallback_text, checklist_skip_code = _build_slack_blocks(summary, enrichment, event_id)

    payload: Dict[str, Any] = {
        "text": fallback_text,
        "blocks": blocks,
    }

    timeout = httpx.Timeout(settings.slack_timeout_seconds)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.post(settings.slack_webhook_url, json=payload)
            resp.raise_for_status()
    except Exception:
        logger.exception("slack webhook post failed")
        return {"sent": False, "error": "post_failed"}

    rca_info = (enrichment or {}).get("rca_ollama") if isinstance(enrichment, dict) else {}
    rca_info = rca_info if isinstance(rca_info, dict) else {}
    rca_text = rca_info.get("text")
    meta: Dict[str, Any] = {"sent": True}
    if isinstance(rca_text, str) and rca_text.strip():
        meta["rca_included"] = True
    else:
        meta["rca_included"] = False
    if checklist_skip_code:
        meta["operator_checklist_skipped"] = checklist_skip_code
    return meta
