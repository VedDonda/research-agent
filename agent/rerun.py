"""Step 6: act on the verification flags and write Pass 2.

For every Pass 1 record:
  round 1: evidence + consistency + critic checks  -> flags per field
  browser: thin pages re-opened in Chromium; apps that gain text are re-researched
  re-research (flagged apps, most-flagged first, while quota lasts):
      one targeted search per flagged field -> fetch new pages ->
      re-extract ONLY the flagged fields -> evidence + critic on what changed
  anything still flagged -> needs_human, with the reason

Pass 1 is read-only. Pass 2 is rewritten on every run (all LLM calls are
cached, so a re-run costs nothing extra).
"""
from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

from pydantic import TypeAdapter
from tqdm import tqdm

from agent import composio_client, llm
from agent.common import read_json, unique
from agent.config import CACHE_DIR, DISCOVER_CACHE, RESULTS_DIR, SETTINGS
from agent.discover import is_official, official_domains
from agent.extract import (MAX_OUTPUT_TOKENS, SYSTEM_PROMPT, ExtractionOutput, _evidenced,
                           _lower, _user_message)
from agent.fetch import build_docs_bundle, count_tokens, fetch_page, load_page, rescue_with_composio
from agent.schema import AppRecord
from agent.verify import (CHECKED_FIELDS, VERIFY_LOG, browser_fetch, consistency_check,
                          critic_check, evidence_check, log_check, recompute_verdict)

log = logging.getLogger("agent.rerun")

PASS1 = RESULTS_DIR / "pass1.jsonl"
PASS2 = RESULTS_DIR / "pass2.jsonl"
RERUN_DOCS = CACHE_DIR / "rerun_docs"
FIELD_QUERIES = {
    "access": "{app} API access requirements developer account approval pricing",
    "auth_methods": "{app} API authentication OAuth API key token",
    "api_type": "{app} API reference REST GraphQL endpoints",
    "mcp": "{app} official MCP server",
}


def load_jsonl(path) -> list[AppRecord]:
    return [AppRecord.model_validate(json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def targeted_pages(app: dict, fields: list[str]) -> list[dict]:
    """One search per flagged field; keep the best 2 new pages per search,
    vendor pages first. Pages are fetched with the same fallback chain."""
    domains = official_domains(app)
    urls = []
    for field in fields:
        try:
            results = composio_client.search(FIELD_QUERIES[field].format(app=app["app"]))
        except Exception as exc:
            log.warning("targeted search failed for %s/%s: %s", app["app"], field, exc)
            continue
        ranked = sorted(results, key=lambda r: (not is_official(r.url, app, domains), -r.score))
        urls += [r.url for r in ranked[:2]]
    pages = [fetch_page(u) for u in unique(urls)]
    return rescue_with_composio(pages)


def reextract(app: dict, rec: AppRecord, flags: dict[str, str]) -> dict:
    """Re-extract only the flagged fields from old + new pages.
    Returns {field: Evidenced} for fields the model answered validly."""
    fields = [f for f in CHECKED_FIELDS if f in flags]
    new_pages = targeted_pages(app, fields)
    old_pages = [p for u in rec.sources if (p := load_page(u))]
    # New pages first, so each gets its best paragraph into the budget.
    pages = [p for p in unique_pages(new_pages + old_pages) if p.get("ok")]
    bundle = build_docs_bundle(app, pages, path=RERUN_DOCS / f"{app['id']}.json")
    sections = bundle["sections"]

    ask = {f: {"previous_value": _plain(getattr(rec, f).value) if getattr(rec, f) else None,
               "why_flagged": flags[f]} for f in fields}
    user = (_user_message(app, sections) +
            f"\n\nRE-CHECK ONLY THESE FIELDS; the first answer was flagged:\n{json.dumps(ask)}\n"
            f"Return a JSON object with ONLY these keys {fields}, in the same format as before.")
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
    # Extractor quota is mostly spent by Pass 1, so re-research may fall
    # back to Gemini, then to the critic's model (different from the
    # extractor; the page notes that this weakens critic independence).
    chain = unique([*SETTINGS.extract_models, *SETTINGS.gemini_models[1:], SETTINGS.critic_model])
    result = llm.chat(chain, messages, json_mode=True, max_tokens=MAX_OUTPUT_TOKENS,
                      est_prompt_tokens=count_tokens(SYSTEM_PROMPT + user))
    data = json.loads(result.text)
    answers = {}
    for f in fields:
        if f not in data:
            continue
        try:
            raw = data[f]
            if isinstance(raw, dict) and "value" in raw:
                raw = {**raw, "value": _lower(raw["value"])}
            claim = TypeAdapter(ExtractionOutput.model_fields[f].annotation).validate_python(raw)
        except Exception:
            continue
        answers[f] = _evidenced(claim, sections)
    return {"answers": answers, "sources": [s["url"] for s in sections], "model": result.model}


def unique_pages(pages: list[dict]) -> list[dict]:
    seen, out = set(), []
    for p in pages:
        if p["url"] not in seen:
            seen.add(p["url"])
            out.append(p)
    return out


def _plain(v):
    return [getattr(x, "value", x) for x in v] if isinstance(v, list) else getattr(v, "value", v)


def browser_round(records: list[AppRecord]) -> set[int]:
    """Re-open every thin source page in Chromium. Returns ids of apps that
    gained readable pages (they get re-researched)."""
    thin = {}
    for rec in records:
        discovered = read_json(DISCOVER_CACHE / f"{rec.id}.json") or {"urls": []}
        for u in discovered["urls"]:
            p = load_page(u["url"])
            if p and p.get("needs_browser"):
                thin.setdefault(rec.id, []).append(u["url"])
    urls = [u for us in thin.values() for u in us]
    if not urls:
        return set()
    try:
        browser_fetch(urls)
    except Exception as exc:
        log.warning("browser loop unavailable: %s", exc)
        return set()
    gained = {i for i, us in thin.items() if any((load_page(u) or {}).get("ok") for u in us)}
    for rec in records:
        if rec.id in gained:
            # Add rescued pages to the record's sources so re-extraction sees them.
            rec.sources = unique([*rec.sources, *[u for u in thin[rec.id] if (load_page(u) or {}).get("ok")]])
            log_check(rec, 1, "*", "browser", "rescued", f"{len(thin[rec.id])} thin page(s) re-rendered")
    return gained


def run_pass2(apps_by_id: dict[int, dict], max_rerun: int = 8) -> list[AppRecord]:
    """max_rerun: cap on re-researched apps (time + free quota). The rest keep
    their flags and go to needs_human."""
    records = load_jsonl(PASS1)
    VERIFY_LOG.unlink(missing_ok=True)  # regenerated each run; every check is logged afresh

    # Round 1: checks on every record.
    flags_by_id: dict[int, dict[str, str]] = {}
    for rec in tqdm(records, desc="verify"):
        app = apps_by_id[rec.id]
        flags = {}
        for check in (evidence_check(rec, 1), consistency_check(rec, 1, app), critic_check(rec, 1)):
            for f, why in check.items():
                if f in CHECKED_FIELDS:
                    flags.setdefault(f, why)
        flags_by_id[rec.id] = flags

    for i in browser_round(records):
        for f in CHECKED_FIELDS:
            flags_by_id[i].setdefault(f, "browser rescued new page text")

    # Re-research, most-flagged apps first, until extractor quota runs out.
    out: list[AppRecord] = []
    order = sorted(records, key=lambda r: (-len(flags_by_id[r.id]), r.id))
    quota_left = True
    reruns = 0
    for rec in tqdm(order, desc="re-research"):
        p2 = rec.model_copy(deep=True)
        p2.pass_, p2.changed_fields = 2, []
        flags = dict(flags_by_id[rec.id])
        attempted = bool(flags and quota_left and reruns < max_rerun)
        if attempted:
            reruns += 1
            try:
                res = reextract(apps_by_id[rec.id], p2, flags)
            except llm.AllModelsExhausted:
                quota_left = False
                res = None
            except Exception as exc:
                log.warning("re-research failed for %s: %s", rec.app, exc)
                res = None
            if res:
                p2.sources = unique([*p2.sources, *res["sources"]])
                for f, ev in res["answers"].items():
                    old = getattr(p2, f)
                    if old is not None and _plain(old.value) == _plain(ev.value) and old.evidence_url:
                        flags.pop(f, None)  # same answer from fresh evidence: confirmed
                        log_check(p2, 2, f, "rerun", "confirmed", "")
                        continue
                    setattr(p2, f, ev)
                    p2.changed_fields.append(f)
                    log_check(p2, 2, f, "rerun", "changed", f"{_plain(old.value) if old else None} -> {_plain(ev.value)}")
                # Round 2 checks on what changed: evidence (code) + critic.
                changed = p2.changed_fields
                if changed:
                    ev_flags = evidence_check(p2, 2)
                    cr_flags = critic_check(p2, 2, changed)
                    for f in changed:
                        if f in ev_flags or f in cr_flags:
                            flags[f] = ev_flags.get(f) or cr_flags.get(f)
                        else:
                            flags.pop(f, None)
        recompute_verdict(p2)
        if flags:
            p2.needs_human = True
            reason = "" if attempted else "not re-researched (quota/time cap)"
            p2.human_note = "; ".join(f"{f}: {why}" for f, why in flags.items()) + (f" [{reason}]" if reason else "")
        out.append(p2)

    out.sort(key=lambda r: r.id)
    PASS2.write_text("".join(json.dumps(r.to_json_dict(), ensure_ascii=False) + "\n" for r in out),
                     encoding="utf-8")
    return out
