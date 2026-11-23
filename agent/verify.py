"""Step 5: the verification loops. Each check returns flags; nothing here
changes an answer. rerun.py acts on the flags.

  1. evidence     (code)   cited URL was fetched and loaded; quote is on the page
  2. consistency  (code)   rule verdict vs LLM verdict; claims vs pre-scan signals
  3. browser      (code)   JS-only pages re-opened in headless Chromium
  4. critic       (Groq)   a different model judges claim + quote pairs

Every check is appended to results/verification_log.jsonl as
{id, app, round, field, check, status, detail}.
"""
from __future__ import annotations

import json
import logging
import re

from rapidfuzz import fuzz

from agent import llm
from agent.common import read_json
from agent.config import PRESCAN_CACHE, PROMPTS_DIR, RESULTS_DIR, SETTINGS
from agent.discover import is_official, official_domains
from agent.fetch import count_tokens, load_page
from agent.rules import rule_verdict
from agent.schema import AppRecord

log = logging.getLogger("agent.verify")

VERIFY_LOG = RESULTS_DIR / "verification_log.jsonl"
CRITIC_PROMPT = (PROMPTS_DIR / "critic.md").read_text(encoding="utf-8")
# The fields we score against hand labels, plus the one-liner. Re-research
# targets these; extras (webhooks, breadth...) are reported but not re-run.
CHECKED_FIELDS = ["auth_methods", "access", "api_type", "mcp"]
EVIDENCE_FIELDS = ["one_liner", *CHECKED_FIELDS]
UNKNOWN = {"unknown", "none_found", None}


def is_unknown(v) -> bool:
    """True for unknown/none_found/None, an empty list, or ['unknown']."""
    if isinstance(v, list):
        return not v or all(x in UNKNOWN for x in v)
    return v in UNKNOWN


def log_check(rec: AppRecord, rnd: int, field: str, check: str, status: str, detail: str = "") -> dict:
    entry = {"id": rec.id, "app": rec.app, "round": rnd, "field": field,
             "check": check, "status": status, "detail": detail[:300]}
    with VERIFY_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _value(rec: AppRecord, field: str):
    ev = getattr(rec, field)
    if ev is None:
        return None
    v = ev.value
    return [x.value for x in v] if isinstance(v, list) else getattr(v, "value", v)


# --- 1. Evidence loop -----------------------------------------------------------

def evidence_check(rec: AppRecord, rnd: int) -> dict[str, str]:
    """Is each non-unknown answer backed by a quote that is really on a page
    we fetched? Returns {field: reason} for failures."""
    flags = {}
    for field in EVIDENCE_FIELDS:
        ev, value = getattr(rec, field), _value(rec, field)
        if ev is None or is_unknown(value):
            continue
        if not ev.evidence_url or ev.evidence_url not in rec.sources:
            flags[field] = "no valid source cited"
        elif not ev.snippet:
            flags[field] = "no quote"
        else:
            page = load_page(ev.evidence_url)
            text = _norm(page["text"]) if page else ""
            quote = _norm(ev.snippet)
            if not page or not page.get("ok"):
                flags[field] = "cited page did not load"
            elif quote not in text and fuzz.partial_ratio(quote, text) < 90:
                flags[field] = "quote not found on cited page"
        status = "fail" if field in flags else "pass"
        log_check(rec, rnd, field, "evidence", status, flags.get(field, ""))
    return flags


# --- 2. Consistency loop -----------------------------------------------------------

def consistency_check(rec: AppRecord, rnd: int, app: dict) -> dict[str, str]:
    """Cross-checks that need no LLM: rules vs LLM, claims vs pre-scan."""
    flags: dict[str, str] = {}
    scan = (read_json(PRESCAN_CACHE / f"{rec.id}.json") or {}).get("signals", {})
    count = lambda name: scan.get(name, {}).get("count", 0)

    access, auth = _value(rec, "access"), _value(rec, "auth_methods") or []
    if rec.llm_verdict and rec.verdict and rec.llm_verdict != rec.verdict:
        flags["access"] = f"rule verdict {rec.verdict.value} != LLM verdict {rec.llm_verdict.value}"
    if access in ("self_serve_free", "free_trial") and (
            count("partner_program") or count("contact_sales") or count("request_access")):
        flags["access"] = "claimed self-serve, but pages mention partner program / contact sales / request access"
    if "oauth2" in auth and not count("oauth2"):
        flags["auth_methods"] = "oauth2 claimed but no oauth/authorize/client_id in any page"
    if _value(rec, "mcp") == "official":
        if not rec.mcp_url or not is_official(rec.mcp_url, app, official_domains(app)):
            flags["mcp"] = "official MCP claimed without an MCP URL on the vendor's own site"
    for field in CHECKED_FIELDS:
        if is_unknown(_value(rec, field)):
            flags.setdefault(field, "value is unknown")
    for field in CHECKED_FIELDS:
        log_check(rec, rnd, field, "consistency", "fail" if field in flags else "pass", flags.get(field, ""))
    return flags


# --- 4. Critic loop -------------------------------------------------------------------

def critic_check(rec: AppRecord, rnd: int, fields: list[str] | None = None) -> dict[str, str]:
    """A different model judges each (claim, quote) pair. Only claims + quotes
    are sent (never pages), so a call stays ~1K tokens."""
    claims = []
    for field in fields or CHECKED_FIELDS:
        ev = getattr(rec, field)
        if ev is None or is_unknown(_value(rec, field)):
            continue
        claims.append({"field": field, "value": _value(rec, field), "quote": ev.snippet})
    if not claims:
        return {}
    user = f"APP: {rec.app}\nCLAIMS:\n{json.dumps(claims, ensure_ascii=False)}"
    messages = [{"role": "system", "content": CRITIC_PROMPT}, {"role": "user", "content": user}]
    try:
        result = llm.chat([SETTINGS.critic_model], messages, json_mode=True, max_tokens=600,
                          est_prompt_tokens=count_tokens(CRITIC_PROMPT + user))
        checks = json.loads(result.text).get("checks", [])
    except Exception as exc:  # critic down/over quota: record it, don't invent a verdict
        log.warning("critic failed for %s: %s", rec.app, exc)
        log_check(rec, rnd, "*", "critic", "error", str(exc))
        return {}
    flags = {}
    for c in checks:
        field, status = c.get("field"), c.get("status")
        if field not in CHECKED_FIELDS:
            continue
        log_check(rec, rnd, field, "critic", status or "?", c.get("reason", ""))
        if status != "supported":
            flags[field] = f"critic: {status} ({c.get('reason', '')})"
    return flags


# --- 3. Browser loop ---------------------------------------------------------------------

def browser_fetch(urls: list[str]) -> int:
    """Re-open thin pages in headless Chromium (real JS rendering) and replace
    their cached text when the browser gets more. Returns pages rescued."""
    import trafilatura
    from playwright.sync_api import sync_playwright

    from agent.fetch import _save_page, is_thin

    rescued = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        for url in urls:
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
                text = trafilatura.extract(page.content(), include_tables=True, favor_recall=True) or ""
            except Exception as exc:
                log.info("browser failed %s: %s", url, exc)
                continue
            if not is_thin(text):
                meta = {k: v for k, v in (load_page(url) or {"url": url}).items() if k != "text"}
                meta.update(method="playwright", chars=len(text), ok=True, needs_browser=False)
                _save_page(meta, text)
                rescued += 1
        browser.close()
    return rescued


def recompute_verdict(rec: AppRecord) -> None:
    """Verdict always follows the rules from the current field values."""
    verdict, reason = rule_verdict(rec.access.value if rec.access else None,
                                   rec.api_type.value if rec.api_type else None,
                                   rec.auth_methods.value if rec.auth_methods else None)
    rec.verdict = verdict
    if verdict is None:
        rec.verdict_reason = reason
