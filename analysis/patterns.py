"""Patterns across the 100 apps, computed with pandas from results/final.json.
Every number on the page comes from here; the headline sentences are Python
templates filled with these numbers (no LLM writes numbers).

    python -m analysis.patterns
"""
from __future__ import annotations

import json
from collections import Counter

import pandas as pd

from agent.config import ROOT
from agent.finalize import FINAL_JSON

PATTERNS_JSON = ROOT / "analysis" / "patterns.json"
SELF_SERVE = {"self_serve_free", "free_trial"}
GATED = {"paid_plan", "admin_approval", "partner_gated", "contact_sales", "no_public_api"}
# The blocker bucket is the access level itself: it's an enum, so counting it
# is exact, unlike the LLM's free-text main_blocker.
BLOCKER_NAMES = {"admin_approval": "app review / approval", "paid_plan": "paid plan",
                 "partner_gated": "partner program", "contact_sales": "contact sales",
                 "no_public_api": "no public API"}


def compute() -> dict:
    data = json.loads(FINAL_JSON.read_text(encoding="utf-8"))
    apps = data["apps"]
    v = lambda r, k: (r.get(k) or {}).get("value")
    df = pd.DataFrame([{
        "id": r["id"], "app": r["app"], "category": r["category"],
        "auth": v(r, "auth_methods") or [], "access": v(r, "access") or "unknown",
        "api": v(r, "api_type") or [], "mcp": v(r, "mcp") or "none_found",
        "verdict": r.get("verdict") or "unknown", "toolkit": r.get("composio_toolkit_exists"),
        "researched": r.get("pass", 1) != 0,
    } for r in apps])
    n_total = len(df)
    # Patterns are computed over researched apps only: unresearched apps
    # (free quota ran out) have no values and would read as "not self-serve".
    not_researched = df.loc[~df["researched"], "app"].tolist()
    df = df[df["researched"]]
    n = len(df)

    auth_counts = Counter(a for auths in df["auth"] for a in auths)
    access_by_cat = pd.crosstab(df["category"], df["access"]).to_dict(orient="index")
    verdict_by_cat = pd.crosstab(df["category"], df["verdict"]).to_dict(orient="index")
    self_serve_share = (df.assign(ss=df["access"].isin(SELF_SERVE))
                          .groupby("category")["ss"].mean().mul(100).round().sort_values())
    blockers = Counter(BLOCKER_NAMES[a] for a in df["access"] if a in BLOCKER_NAMES)
    easy = df[(df["verdict"] == "ready") & (df["toolkit"] == False)]  # noqa: E712 (None != False)
    outreach = df[df["access"].isin({"partner_gated", "contact_sales"})]
    verdicts = Counter(df["verdict"])
    mcp = Counter(df["mcp"])

    top_auth, top_auth_n = auth_counts.most_common(1)[0] if auth_counts else ("-", 0)
    oauth_and_key = sum(1 for a in df["auth"] if "oauth2" in a and "api_key" in a)
    top_blocker, top_blocker_n = blockers.most_common(1)[0] if blockers else ("-", 0)
    headlines = [
        f"{verdicts.get('ready', 0)} of {n} apps could be agent toolkits today with self-serve credentials; "
        f"{verdicts.get('ready_with_friction', 0)} need friction (paid plan or app review) and "
        f"{verdicts.get('blocked', 0)} are blocked.",
        f"{top_auth.replace('_', ' ')} is the most common auth method ({top_auth_n} of {n} apps); "
        f"{oauth_and_key} apps offer both OAuth2 and API keys.",
        f"The most common blocker is {top_blocker} ({top_blocker_n} apps). "
        f"{len(outreach)} apps need outreach (partner program or contact sales).",
        f"Most self-serve: {self_serve_share.index[-1]} ({int(self_serve_share.iloc[-1])}% self-serve). "
        f"Most gated: {self_serve_share.index[0]} ({int(self_serve_share.iloc[0])}% self-serve).",
        f"{len(easy)} easy wins: ready today and no Composio toolkit yet.",
        f"{mcp.get('official', 0)} apps have an official MCP server, {mcp.get('community', 0)} only community ones, "
        f"{mcp.get('none_found', 0)} none found.",
    ]
    headlines.insert(0, f"{n} of {n_total} apps were researched; the free LLM quota ran out before the other "
                        f"{len(not_researched)}. All patterns below are over the {n} researched apps.")
    out = {
        "n": n, "n_total": n_total, "not_researched": not_researched, "headlines": headlines,
        "auth_counts": dict(auth_counts.most_common()),
        "access_counts": dict(Counter(df["access"]).most_common()),
        "access_by_category": access_by_cat, "verdict_by_category": verdict_by_cat,
        "self_serve_pct_by_category": self_serve_share.to_dict(),
        "verdict_counts": dict(verdicts), "blockers": dict(blockers.most_common()), "mcp_counts": dict(mcp),
        "easy_wins": easy[["id", "app", "category"]].to_dict(orient="records"),
        "needs_outreach": outreach[["id", "app", "category", "access"]].to_dict(orient="records"),
        "overrides": data.get("overrides", []),
    }
    PATTERNS_JSON.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    return out


if __name__ == "__main__":
    for h in compute()["headlines"]:
        print("-", h)
