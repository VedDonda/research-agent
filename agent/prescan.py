"""Step 3: keyword signals over each app's FULL fetched pages.
No LLM.

Why: it's an independent, dumb-but-honest second opinion. In Phase 4 the
rule loop compares it with the LLM's answers, e.g. the LLM says "self-serve"
but the pages say "partner program" and "apply": flag it. It runs on full
pages, not the trimmed bundle, so it also catches signals the trim dropped.

Usage:
    python -m agent.prescan --all
"""
from __future__ import annotations

import argparse
import re

from tqdm import tqdm

from agent.common import add_app_selection_args, read_json, select_apps, write_json
from agent.config import DISCOVER_CACHE, PRESCAN_CACHE, setup_logging
from agent.fetch import load_page

# signal name -> regex. Grouped by the schema field each one cross-checks.
SIGNALS: dict[str, str] = {
    # auth_methods
    "oauth2": r"oauth|/authorize\b|client[_ ]id",
    "api_key": r"api[_ -]?key|x-api-key",
    "basic": r"basic auth",
    "bearer_token": r"bearer|personal access token",
    "jwt": r"\bjwt\b|json web token",
    "hmac_signature": r"\bhmac\b|signature",
    # access / gating
    "contact_sales": r"contact (?:sales|us)|talk to (?:sales|an expert)|book a demo",
    "request_access": r"request access|apply for access|waitlist",
    "partner_program": r"partner program|partnership|become a partner",
    "approval": r"approval|app review|security review|be approved|verified app",
    "free_plan": r"free plan|free tier|free account|free forever|sign up (?:for )?free|no credit card",
    "trial": r"free trial|\btrial\b",
    "paid_plan": r"paid plan|enterprise plan|upgrade to|available on .{0,20}plans?",
    # api surface
    "graphql": r"graphql",
    "rest": r"\brest(?:ful)? api\b|\bget /|\bpost /|endpoint",
    "mcp": r"\bmcp\b|model context protocol",
    "webhooks": r"webhook",
    "sandbox": r"sandbox|test mode|developer (?:account|environment|org)",
    "rate_limit": r"rate[ -]?limit",
}
_COMPILED = {name: re.compile(rx, re.I) for name, rx in SIGNALS.items()}


def _context(text: str, match: re.Match, width: int = 80) -> str:
    """A short excerpt around a match, so a human can see why it fired."""
    start, end = max(match.start() - width, 0), min(match.end() + width, len(text))
    return " ".join(text[start:end].split())


def prescan_app(app: dict) -> dict:
    """Count each signal across the app's good pages; keep 1 example per signal."""
    discovered = read_json(DISCOVER_CACHE / f"{app['id']}.json") or {"urls": []}
    pages = [p for u in discovered["urls"] if (p := load_page(u["url"])) and p.get("ok")]

    signals = {}
    for name, rx in _COMPILED.items():
        count, urls, example = 0, [], None
        for page in pages:
            matches = list(rx.finditer(page["text"]))
            if matches:
                count += len(matches)
                urls.append(page["url"])
                example = example or {"url": page["url"], "text": _context(page["text"], matches[0])}
        signals[name] = {"count": count, "urls": urls, "example": example}

    result = {"id": app["id"], "app": app["app"], "pages_scanned": len(pages),
              "present": sorted(n for n, s in signals.items() if s["count"]),
              "signals": signals}
    write_json(PRESCAN_CACHE / f"{app['id']}.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Step 3: regex pre-scan")
    add_app_selection_args(parser)
    args = parser.parse_args()
    setup_logging()
    for app in tqdm(select_apps(args), desc="prescan"):
        res = prescan_app(app)
        tqdm.write(f"{app['id']:>3} {app['app']:<26} {', '.join(res['present']) or '-'}")


if __name__ == "__main__":
    main()
