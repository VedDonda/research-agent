"""Build the single self-contained HTML page: site/index.html, plus
results.json / results.csv copies next to it for download.

    python site/build.py            (after agent.finalize, analysis.patterns, eval.score)

No framework and no external JS: the data is embedded as JSON in the page
(<script id="data">), so humans get a readable page and agents get the
whole dataset from the same URL.
"""
from __future__ import annotations

import html
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.finalize import FINAL_CSV, FINAL_JSON  # noqa: E402

SITE = ROOT / "site"
REPO_URL = "https://github.com/VedDonda/research-agent"
e = html.escape


def bar_rows(counts: dict, total: int) -> str:
    peak = max(counts.values() or [1])
    return "".join(
        f'<div class="bar"><span class="lbl">{e(str(k).replace("_", " "))}</span>'
        f'<span class="track"><span class="fill" style="width:{100 * v / peak:.0f}%"></span></span>'
        f'<span class="num">{v}</span></div>' for k, v in counts.items())


def heatmap(matrix: dict, cols: list[str]) -> str:
    head = "".join(f"<th>{e(c.replace('_', ' '))}</th>" for c in cols)
    rows = []
    for cat, vals in sorted(matrix.items()):
        cells = "".join(
            f'<td style="background:rgba(37,99,235,{min(vals.get(c, 0) / 6, 1):.2f})">{vals.get(c, 0) or ""}</td>'
            for c in cols)
        rows.append(f"<tr><th class='cat'>{e(cat)}</th>{cells}</tr>")
    return f"<table class='heat'><tr><th></th>{head}</tr>{''.join(rows)}</table>"


def accuracy_table(scores: dict) -> str:
    passes = scores.get("passes", {})
    if not passes:
        return "<p>Scores not computed yet.</p>"
    fields = list(next(iter(passes.values()))["fields"])
    head = "".join(f"<th>{p}</th>" for p in passes)
    rows = "".join(
        f"<tr><td>{f.replace('_', ' ')}</td>" +
        "".join(f"<td>{passes[p]['fields'][f]['exact']}%</td>" for p in passes) + "</tr>" for f in fields)
    overall = "".join(f"<td><b>{passes[p]['overall']}%</b></td>" for p in passes)
    hold = "".join(f"<td>{passes[p]['holdout']}% / {passes[p]['tuned']}%</td>" for p in passes)
    return (f"<table class='acc'><tr><th>field</th>{head}</tr>{rows}"
            f"<tr><td><b>overall</b></td>{overall}</tr><tr><td>holdout / tuned</td>{hold}</tr></table>")


def hitmiss_table(scores: dict) -> str:
    fields = ["auth_methods", "access", "api_type", "mcp", "verdict"]
    head = "".join(f"<th>{f.replace('_', ' ')}</th>" for f in fields)
    rows = []
    for a in scores.get("apps", []):
        cells = []
        for f in fields:
            c = a["fields"][f]
            mark = "✅" if c["hit"] else "❌"
            title = f"truth: {c['truth']} | agent: {c['agent']} | pass1: {c['pass1']}"
            body = "" if c["hit"] else f"<small>{e(str(c['agent']))} ≠ {e(str(c['truth']))}</small>"
            cells.append(f'<td title="{e(title)}">{mark} {body}</td>')
        star = " ★" if a["holdout"] else ""
        rows.append(f"<tr><td>{e(a['app'])}{star}</td>{''.join(cells)}</tr>")
    return f"<table class='hm'><tr><th>app (★ holdout)</th>{head}</tr>{''.join(rows)}</table>"


def loop_stats() -> str:
    log = ROOT / "results" / "verification_log.jsonl"
    if not log.exists():
        return ""
    from collections import Counter
    c = Counter()
    for line in log.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        c[(r["check"], r["status"])] += 1
    checks = ["evidence", "consistency", "critic", "browser", "rerun"]
    rows = []
    for ch in checks:
        statuses = {s: n for (k, s), n in c.items() if k == ch}
        if statuses:
            rows.append(f"<tr><td>{ch}</td><td>{', '.join(f'{s}: {n}' for s, n in sorted(statuses.items()))}</td></tr>")
    return f"<table class='acc'><tr><th>loop</th><th>results (field checks)</th></tr>{''.join(rows)}</table>"


def main() -> None:
    final = json.loads(FINAL_JSON.read_text(encoding="utf-8"))
    patterns = json.loads((ROOT / "analysis" / "patterns.json").read_text(encoding="utf-8"))
    scores_path = ROOT / "eval" / "scores.json"
    scores = json.loads(scores_path.read_text(encoding="utf-8")) if scores_path.exists() else {}
    friction = (ROOT / "docs" / "composio_friction.md").read_text(encoding="utf-8")

    shutil.copy(FINAL_JSON, SITE / "results.json")
    shutil.copy(FINAL_CSV, SITE / "results.csv")

    access_cols = ["self_serve_free", "free_trial", "paid_plan", "admin_approval",
                   "partner_gated", "contact_sales", "no_public_api", "unknown"]
    access_cols = [c for c in access_cols if any(c in v for v in patterns["access_by_category"].values())]
    needs_human = [a for a in final["apps"] if a.get("needs_human")]
    friction_rows = [l for l in friction.splitlines() if l.startswith("| ") and not l.startswith("| #")]

    page = TEMPLATE
    subs = {
        "{{N}}": str(patterns["n"]),
        "{{REPO}}": REPO_URL,
        "{{HEADLINES}}": "".join(f"<li>{e(h)}</li>" for h in patterns["headlines"]),
        "{{AUTH_BARS}}": bar_rows(patterns["auth_counts"], patterns["n"]),
        "{{VERDICT_BARS}}": bar_rows(patterns["verdict_counts"], patterns["n"]),
        "{{BLOCKER_BARS}}": bar_rows(patterns["blockers"], patterns["n"]),
        "{{HEATMAP}}": heatmap(patterns["access_by_category"], access_cols),
        "{{EASY}}": "".join(f"<li>{e(a['app'])} <small>{e(a['category'])}</small></li>" for a in patterns["easy_wins"]),
        "{{OUTREACH}}": "".join(f"<li>{e(a['app'])} <small>{e(a['access'].replace('_', ' '))}</small></li>"
                                for a in patterns["needs_outreach"]),
        "{{ACCURACY}}": accuracy_table(scores),
        "{{HITMISS}}": hitmiss_table(scores),
        "{{LOOPS}}": loop_stats(),
        "{{NEEDS_HUMAN}}": "".join(f"<li><b>{e(a['app'])}</b>: {e(a.get('human_note') or '')}</li>"
                                   for a in needs_human[:40]),
        "{{N_HUMAN}}": str(len(needs_human)),
        "{{OVERRIDES}}": "".join(f"<li><b>{e(o['app'])}</b> {e(o['field'])}: {e(str(o['from']))} → "
                                 f"{e(str(o['to']))} ({e(o['note'])})</li>" for o in final.get("overrides", []))
                         or "<li>None recorded.</li>",
        "{{FRICTION}}": "".join(f"<li>{e(r.split('|')[2].strip())}</li>" for r in friction_rows),
        "{{DATA}}": json.dumps(final["apps"], ensure_ascii=False).replace("</", "<\\/"),
    }
    for k, val in subs.items():
        page = page.replace(k, val)
    (SITE / "index.html").write_text(page, encoding="utf-8")
    print(f"wrote {SITE / 'index.html'} ({len(page) // 1024} KB)")


TEMPLATE = (SITE / "template.html").read_text(encoding="utf-8")

if __name__ == "__main__":
    main()
