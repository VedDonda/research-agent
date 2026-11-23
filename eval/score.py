"""Evaluation: build the 20-app sample now; score the agent against it later.

Usage:
    python -m eval.score --make-sample   # print the 20 sampled apps (+ blank ground_truth.csv if missing)
    python -m eval.score                  # Phase 5: accuracy report (not built yet)

The sample is never stored: make_sample() rebuilds the exact same 20 apps and
holdout split from the fixed seed every time, so there's no file to drift.

Why a sample at all: checking 100 apps by hand is too slow, but checking 20
chosen fairly (2 per category, fixed seed) gives an honest accuracy estimate
that anyone can reproduce.
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from agent.config import APPS_CSV, EVAL_DIR

SEED = 42                # fixed, so the same 20 apps come out every time
PER_CATEGORY = 2         # 10 categories x 2 = 20 apps
GROUND_TRUTH_CSV = EVAL_DIR / "ground_truth.csv"

# Columns the human fills in. `hint` is included only to
# make labelling faster; the scorer ignores it.
GROUND_TRUTH_COLUMNS = [
    "id", "app", "hint",
    "auth_methods",   # semicolon list, e.g. "oauth2;api_key"
    "access", "api_type", "mcp", "verdict",
    "evidence_url", "notes",
]


def make_sample() -> pd.DataFrame:
    """Stratified random sample: PER_CATEGORY apps from each category.

    Stratifying matters: a plain random 20 could skip a whole category, and
    categories differ a lot (fintech is gated far more often than dev tools).
    """
    apps = pd.read_csv(APPS_CSV)
    sample = apps.groupby("category", sort=True).sample(n=PER_CATEGORY, random_state=SEED)

    # Holdout: one app per category (the first one drawn). Prompts may be tuned
    # by looking at the other 10, never at these, so the holdout score shows
    # whether tuning just overfitted to the apps we looked at.
    sample["holdout"] = sample.groupby("category").cumcount() == 0
    return sample.sort_values("id").reset_index(drop=True)


def holdout_ids() -> set[int]:
    """Ids of the 10 holdout apps (never looked at while tuning prompts)."""
    sample = make_sample()
    return set(sample.loc[sample["holdout"], "id"])


def show_sample() -> None:
    """Print the sample. Writes a blank ground_truth.csv only if none exists;
    an existing one holds hand labels and is never touched."""
    sample = make_sample()
    if not GROUND_TRUTH_CSV.exists():
        EVAL_DIR.mkdir(exist_ok=True)
        blank = sample[["id", "app", "hint"]].copy()
        for col in GROUND_TRUTH_COLUMNS[3:]:
            blank[col] = ""
        blank[GROUND_TRUTH_COLUMNS].to_csv(GROUND_TRUTH_CSV, index=False)
        print(f"Wrote blank {GROUND_TRUTH_CSV}")
    print(sample[["id", "app", "category", "holdout"]].to_string(index=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--make-sample", action="store_true", help="print the 20-app sample")
    args = parser.parse_args()

    if args.make_sample:
        show_sample()
        return 0
    score_all()
    return 0


# --- Scoring --------------------------------------------------------------------

SCORED = ["auth_methods", "access", "api_type", "mcp", "verdict"]
SET_FIELDS = {"auth_methods", "api_type"}   # compared as sets: order doesn't matter
SCORES_JSON = EVAL_DIR / "scores.json"


def _split(v) -> set[str]:
    return {x.strip() for x in str(v or "").split(";") if x.strip()}


def agent_value(rec: dict | None, field: str):
    if rec is None:
        return None
    if field == "verdict":
        return rec.get("verdict")
    v = (rec.get(field) or {}).get("value")
    return ";".join(sorted(v)) if isinstance(v, list) else v


def compare(field: str, truth, got) -> tuple[bool, float]:
    """-> (exact hit, partial credit). Partial = Jaccard overlap for set fields."""
    if field in SET_FIELDS:
        t, g = _split(truth), _split(got)
        if not t and not g:
            return True, 1.0
        return t == g, len(t & g) / len(t | g) if t | g else 0.0
    hit = str(truth or "").strip() == str(got or "").strip()
    return hit, float(hit)


def load_pass(path) -> dict[int, dict]:
    import json
    if not path.exists():
        return {}
    if path.suffix == ".json":
        return {r["id"]: r for r in json.loads(path.read_text(encoding="utf-8"))["apps"]}
    return {json.loads(l)["id"]: json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()}


def score_all() -> dict:
    import json
    from agent.config import RESULTS_DIR

    truth = pd.read_csv(GROUND_TRUTH_CSV).fillna("")
    holdout = holdout_ids()
    passes = {"pass1": load_pass(RESULTS_DIR / "pass1.jsonl"),
              "pass2": load_pass(RESULTS_DIR / "pass2.jsonl"),
              "final": load_pass(RESULTS_DIR / "final.json")}
    report = {"n_apps": len(truth), "holdout_ids": sorted(int(i) for i in holdout), "passes": {}, "apps": []}

    for name, recs in passes.items():
        if not recs:
            continue
        per_field = {}
        for field in SCORED:
            rows = [(int(t["id"]) in holdout, *compare(field, t[field], agent_value(recs.get(int(t["id"])), field)))
                    for _, t in truth.iterrows()]
            pct = lambda xs: round(100 * sum(xs) / len(xs), 1) if xs else None
            per_field[field] = {
                "exact": pct([h for _, h, _ in rows]),
                "partial": pct([p for _, _, p in rows]),
                "holdout": pct([h for ho, h, _ in rows if ho]),
                "tuned": pct([h for ho, h, _ in rows if not ho]),
            }
        overall = round(sum(f["exact"] for f in per_field.values()) / len(SCORED), 1)
        report["passes"][name] = {"fields": per_field, "overall": overall,
                                  "holdout": round(sum(f["holdout"] for f in per_field.values()) / len(SCORED), 1),
                                  "tuned": round(sum(f["tuned"] for f in per_field.values()) / len(SCORED), 1)}

    # Per-app hit/miss table for the latest pass available.
    latest = next((n for n in ("final", "pass2", "pass1") if passes[n]), None)
    for _, t in truth.iterrows():
        rec = passes[latest].get(int(t["id"])) if latest else None
        cells = {}
        for field in SCORED:
            got = agent_value(rec, field)
            cells[field] = {"truth": t[field], "agent": got, "hit": compare(field, t[field], got)[0],
                            "pass1": agent_value(passes["pass1"].get(int(t["id"])), field)}
        report["apps"].append({"id": int(t["id"]), "app": t["app"], "holdout": int(t["id"]) in holdout,
                               "notes": t.get("notes", ""), "fields": cells})
    report["latest"] = latest
    SCORES_JSON.write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str), encoding="utf-8")

    for name, p in report["passes"].items():
        print(f"{name:<6} overall {p['overall']:>5}%  (holdout {p['holdout']}%, tuned {p['tuned']}%)  " +
              "  ".join(f"{f} {v['exact']}%" for f, v in p["fields"].items()))
    return report


if __name__ == "__main__":
    sys.exit(main())
