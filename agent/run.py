"""CLI entry point. Grows phase by phase (see README, Commands).

Phase 2:
    python -m agent.run --all --prep      # discover + fetch + prescan + toolkit check, then report
    python -m agent.run --app Linear --prep
Phase 3:
    python -m agent.run --app Linear --extract   # extract one app, print JSON, write nothing
    python -m agent.run --all --pass 1           # extract all -> results/pass1.jsonl (append-only)

Everything is cached and resumable: re-running only does work that is missing.
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import Counter

import pandas as pd
from tqdm import tqdm

from agent import composio_client
from agent import llm
from agent.common import add_app_selection_args, select_apps, write_json
from agent.config import RESULTS_DIR, setup_logging
from agent.discover import discover_app
from agent.extract import extract_app
from agent.fetch import fetch_app
from agent.prescan import prescan_app

log = logging.getLogger("agent.run")
PREP_REPORT = RESULTS_DIR / "phase2_report.csv"
TOOLKIT_MATCH = RESULTS_DIR / "composio_toolkit_match.json"
PASS1 = RESULTS_DIR / "pass1.jsonl"


def prep(apps: list[dict], force: bool) -> pd.DataFrame:
    """Steps 1-3 for each app, plus the Composio catalogue check. No LLM."""
    catalogue = composio_client.list_composio_toolkits()
    rows, matches = [], {}
    for app in tqdm(apps, desc="prep"):
        try:
            discovered = discover_app(app, force)
            pages = fetch_app(app, force)
            scan = prescan_app(app)
        except Exception as exc:  # keep going; the report shows the failure
            log.exception("prep failed for %s", app["app"])
            rows.append({"id": app["id"], "app": app["app"], "error": str(exc)[:200]})
            continue
        match = composio_client.match_toolkits(app["app"], catalogue)
        matches[app["id"]] = {"app": app["app"], **match}
        good = [p for p in pages if p["ok"]]
        rows.append({
            "id": app["id"], "app": app["app"], "category": app["category"],
            "urls": len(discovered["urls"]),
            "good_pages": len(good),
            "needs_browser": sum(p["needs_browser"] for p in pages),
            "rescued_by_composio": sum(p["method"] != "httpx" for p in good),
            "good_chars": sum(p["chars"] for p in good),
            "signals": ";".join(scan["present"]),
            "composio_toolkit": ";".join(match["exact"]),
            "composio_near": ";".join(match["near"]),
            "error": "",
        })
    df = pd.DataFrame(rows)
    RESULTS_DIR.mkdir(exist_ok=True)
    df.to_csv(PREP_REPORT, index=False)
    write_json(TOOLKIT_MATCH, matches)
    return df


def print_prep_summary(df: pd.DataFrame) -> None:
    ok = df[df["error"] == ""]
    weak = ok[ok["good_pages"] < 2]
    print(f"\nApps prepared: {len(ok)}/{len(df)}   report: {PREP_REPORT}")
    print(f"Good pages per app: median {ok['good_pages'].median():.0f}, "
          f"total {ok['good_pages'].sum()}  |  rescued by Composio extract: "
          f"{ok['rescued_by_composio'].sum()}  |  still needs browser: {ok['needs_browser'].sum()}")
    has_tk = (ok["composio_toolkit"] != "").sum()
    print(f"Composio already has a toolkit (exact name match): {has_tk}/{len(ok)}")
    signal_counts = Counter(s for sig in ok["signals"] for s in sig.split(";") if s)
    print("Apps with each signal:", ", ".join(f"{k} {v}" for k, v in signal_counts.most_common()))
    if len(weak):
        print(f"\nApps with < 2 good pages ({len(weak)}): they will need the browser loop or a human")
        print(weak[["id", "app", "urls", "good_pages", "needs_browser"]].to_string(index=False))
    failed = df[df["error"] != ""]
    if len(failed):
        print(f"\nFailed ({len(failed)}):")
        print(failed[["id", "app", "error"]].to_string(index=False))


def done_ids(path) -> set[int]:
    """Ids already written to a results file (that's what makes runs resumable)."""
    if not path.exists():
        return set()
    return {json.loads(line)["id"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def run_pass1(apps: list[dict]) -> None:
    """Extract every app not yet in pass1.jsonl and APPEND it.

    pass1.jsonl is never rewritten: Pass 1 is the frozen "before" snapshot
    that the verification loops are measured against. To redo an app you'd
    delete its line by hand, on purpose. There is deliberately no --force.
    """
    todo = [a for a in apps if a["id"] not in done_ids(PASS1)]
    print(f"Pass 1: {len(apps) - len(todo)} already done, {len(todo)} to extract")
    failures = []
    PASS1.parent.mkdir(exist_ok=True)
    for app in tqdm(todo, desc="extract"):
        try:
            record = extract_app(app)
        except llm.AllModelsExhausted as exc:
            print(f"\nStopping: every extractor model is out of quota for today.\n{exc}\n"
                  "Re-run the same command tomorrow; it resumes where it stopped.")
            break
        except Exception as exc:
            log.exception("extract failed for %s", app["app"])
            failures.append((app["app"], str(exc)[:150]))
            continue
        with PASS1.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record.to_json_dict(), ensure_ascii=False) + "\n")
        tqdm.write(f"{app['id']:>3} {app['app']:<26} {record.verdict.value if record.verdict else 'unknown':<20}"
                   f" access={record.access.value.value}  by {record.extractor_model}")
    print_llm_summary(failures)


def print_llm_summary(failures: list) -> None:
    usage = llm.usage_today()
    print("\nLLM usage today:")
    for model, m in usage["models"].items():
        print(f"  {model:<24} calls {m['calls']:>4}  cache hits {m['cache_hits']:>4}  tokens {m['tokens']:>8}")
    if usage["exhausted"]:
        print("  daily cap reached:", ", ".join(usage["exhausted"]))
    if failures:
        print(f"Failures ({len(failures)}):")
        for name, err in failures:
            print(f"  {name}: {err}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_app_selection_args(parser)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prep", action="store_true", help="Phase 2: discover + fetch + prescan")
    mode.add_argument("--extract", action="store_true", help="extract and print JSON; writes nothing")
    mode.add_argument("--pass", dest="pass_no", type=int, choices=[1], help="1: write results/pass1.jsonl")
    mode.add_argument("--verify", action="store_true", help="Phase 4: checks + re-research -> results/pass2.jsonl")
    args = parser.parse_args()
    setup_logging()
    apps = select_apps(args)
    if args.prep:
        print_prep_summary(prep(apps, args.force))
    elif args.extract:
        for app in apps:
            print(json.dumps(extract_app(app).to_json_dict(), indent=2, ensure_ascii=False))
        print_llm_summary([])
    elif args.pass_no == 1:
        run_pass1(apps)
    elif args.verify:
        from agent.rerun import run_pass2
        records = run_pass2({a["id"]: a for a in apps})
        print(f"Pass 2: {len(records)} records, {sum(bool(r.changed_fields) for r in records)} changed, "
              f"{sum(r.needs_human for r in records)} need a human")
        print_llm_summary([])


if __name__ == "__main__":
    main()
