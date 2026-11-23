"""Make the final dataset: Pass 2 + human overrides -> results/final.json/.csv.

Overrides live in eval/human_overrides.csv: id, field, value, evidence_url, note.
A list field (auth_methods, api_type) takes a semicolon list. Every override
is kept in final.json under "overrides" so the page can list them.

    python -m agent.finalize
"""
from __future__ import annotations

import json

import pandas as pd

from agent.config import EVAL_DIR, RESULTS_DIR
from agent.rerun import PASS1, PASS2, load_jsonl
from agent.schema import Evidenced
from agent.verify import recompute_verdict

OVERRIDES = EVAL_DIR / "human_overrides.csv"
FINAL_JSON = RESULTS_DIR / "final.json"
FINAL_CSV = RESULTS_DIR / "final.csv"
LIST_FIELDS = {"auth_methods", "api_type"}


def flat(rec: dict) -> dict:
    """One row per app for the CSV and the page table."""
    v = lambda k: (rec.get(k) or {}).get("value")
    j = lambda x: ";".join(x) if isinstance(x, list) else x
    return {
        "id": rec["id"], "app": rec["app"], "category": rec["category"],
        "one_liner": v("one_liner"), "auth_methods": j(v("auth_methods")), "access": v("access"),
        "api_type": j(v("api_type")), "api_breadth": v("api_breadth"), "mcp": v("mcp"),
        "mcp_url": rec.get("mcp_url"), "webhooks": v("webhooks"),
        "sandbox_or_test_mode": v("sandbox_or_test_mode"),
        "composio_toolkit_exists": rec.get("composio_toolkit_exists"),
        "verdict": rec.get("verdict"), "main_blocker": rec.get("main_blocker"),
        "verdict_reason": rec.get("verdict_reason"), "confidence": rec.get("confidence"),
        "needs_human": rec.get("needs_human"), "changed_in_pass2": ";".join(rec.get("changed_fields") or []),
        "access_evidence": (rec.get("access") or {}).get("evidence_url"),
        "auth_evidence": (rec.get("auth_methods") or {}).get("evidence_url"),
        "extractor_model": rec.get("extractor_model"),
    }


def main() -> None:
    records = load_jsonl(PASS2 if PASS2.exists() else PASS1)
    by_id = {r.id: r for r in records}
    applied = []
    if OVERRIDES.exists():
        for o in pd.read_csv(OVERRIDES).fillna("").to_dict(orient="records"):
            rec = by_id.get(int(o["id"]))
            if rec is None:
                continue
            value = [x.strip() for x in str(o["value"]).split(";") if x.strip()] \
                if o["field"] in LIST_FIELDS else str(o["value"]).strip()
            old = getattr(rec, o["field"])
            setattr(rec, o["field"], Evidenced(value=value, evidence_url=o["evidence_url"] or None,
                                               snippet=f"[human] {o['note']}"[:300], confidence=1.0))
            recompute_verdict(rec)
            rec.needs_human = False
            rec.human_note = f"overridden by human: {o['note']}"
            applied.append({"id": rec.id, "app": rec.app, "field": o["field"],
                            "from": getattr(old, "value", None) if old else None,
                            "to": value, "evidence_url": o["evidence_url"], "note": o["note"]})
    data = [r.to_json_dict() for r in sorted(by_id.values(), key=lambda r: r.id)]
    FINAL_JSON.write_text(json.dumps({"apps": data, "overrides": applied}, indent=1, ensure_ascii=False,
                                     default=str), encoding="utf-8")
    pd.DataFrame([flat(r) for r in data]).to_csv(FINAL_CSV, index=False)
    print(f"final: {len(data)} apps, {len(applied)} human overrides -> {FINAL_JSON}")


if __name__ == "__main__":
    main()
