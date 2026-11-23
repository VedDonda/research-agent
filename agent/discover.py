"""Step 1: find the few pages that answer our questions for one app.
No LLM: just search plus simple ranking rules.

Three sources of candidate URLs:
  1. the hint from the assignment (usually the official docs URL)
  2. links on that hint page whose path looks like api/auth/pricing/partner
  3. Composio -> Tavily web searches (4 queries per app)

Then simple, explainable rules rank them, and we keep the top MAX_URLS.

Usage:
    python -m agent.discover --all
    python -m agent.discover --app Linear
"""
from __future__ import annotations

import argparse
import logging
import re
from urllib.parse import urlparse

from tqdm import tqdm

from agent import composio_client
from agent.common import add_app_selection_args, read_json, select_apps, unique, write_json
from agent.config import DISCOVER_CACHE, SETTINGS, setup_logging
from agent.fetch import fetch_page, normalize_url

log = logging.getLogger("agent.discover")

MAX_URLS = 6
QUERIES = [  # one Tavily credit each
    "{app} API authentication",
    "{app} developer API docs",
    "{app} API pricing access partner program",
    "{app} MCP server",
]
# Paths with these words are likely to answer auth / access / API questions.
PATH_WORDS = re.compile(
    r"api|developer|docs|reference|auth|oauth|token|key|mcp|pricing|plan|partner|"
    r"rate-?limit|getting-started|quickstart|integrat", re.I)
SKIP_EXTENSIONS = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|zip|gz|mp4|woff2?)$", re.I)
# Never official docs; SEO, forums, video, social, review sites.
BLOCKED_HOSTS = {
    "medium.com", "reddit.com", "stackoverflow.com", "quora.com", "youtube.com",
    "linkedin.com", "g2.com", "capterra.com", "x.com", "twitter.com", "dev.to",
    "apitracker.io", "rapidapi.com", "zapier.com", "make.com", "pipedream.com",
    "n8n.io", "getapp.com", "trustradius.com", "softwareadvice.com",
}


# --- Official domains ---------------------------------------------------------

def registrable(host: str) -> str:
    """'developers.facebook.com' -> 'facebook.com'; 'foo.co.uk' -> 'foo.co.uk'.
    A simple rule, good enough for 100 known apps (no public-suffix library)."""
    parts = host.lower().removeprefix("www.").split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def hint_urls(hint: str) -> list[str]:
    """Domain-like tokens in the hint -> URLs.
    'harvestapp.com (help.getharvest.com/api-v2)' -> both sites."""
    tokens = re.findall(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:/[^\s)]*)?", hint.lower())
    return unique(normalize_url("https://" + t) for t in tokens)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def official_domains(app: dict) -> set[str]:
    """Domains we trust as the vendor's own: the hint's domains, plus the
    app's name as a domain label (e.g. 'linear' -> linear.app found in search)."""
    domains = {registrable(urlparse(u).hostname or "") for u in hint_urls(app["hint"])}
    return {d for d in domains if d} - {"github.io"}  # github.io hosts anyone's site


def _repo_owner(url: str) -> str:
    """'https://github.com/sherlock-project/sherlock' -> 'sherlock-project'."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[0].lower() if parts else ""


def is_official(url: str, app: dict, domains: set[str]) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:  # malformed URL (e.g. an LLM-written mcp_url): not official
        return False
    # github.com hosts everyone's repos, so the domain alone proves nothing:
    # a repo is official only if its owner is the hint's owner (Sherlock ->
    # sherlock-project) or the app itself (GitHub -> github).
    if host == "github.com":
        hint_owners = {_repo_owner(u) for u in hint_urls(app["hint"])
                       if urlparse(u).hostname == "github.com"}
        owner = _repo_owner(url)
        return bool(owner) and (owner in hint_owners or _norm(owner) == _norm(app["app"]))
    if registrable(host) in domains:
        return True
    # Name match (e.g. Mermaid CLI -> mermaid.ai): only when the hint names
    # no website other than a GitHub repo, since otherwise same-name products
    # on other domains would count as the vendor's.
    if domains - {"github.com"}:
        return False
    label = registrable(host).split(".")[0]
    name_forms = {_norm(app["app"]), _norm(re.sub(r"\(.*?\)", "", app["app"])),
                  _norm(app["app"].split()[0])}
    return len(label) >= 4 and label in name_forms


# --- Ranking --------------------------------------------------------------------

def score_url(url: str, app: dict, domains: set[str], search_score: float = 0.0) -> float:
    """Higher = more likely to be the official page that answers our questions.
    Every term is a plain rule, so a ranking can be explained in one sentence."""
    host = registrable(urlparse(url).hostname or "")
    if host in BLOCKED_HOSTS:
        return -10
    score = 5.0 if is_official(url, app, domains) else 0.0
    score += min(len(PATH_WORDS.findall(urlparse(url).path)), 4)  # up to +4 for docs-like paths
    score += 2 * search_score                                     # Tavily relevance, 0..2
    return score


def hint_page_links(seed: str, app: dict, domains: set[str]) -> list[str]:
    """Docs-looking links on the hint page itself (same site only)."""
    page = fetch_page(seed)
    return [u for u in page.get("links", [])
            if is_official(u, app, domains) and PATH_WORDS.search(urlparse(u).path)]


def discover_app(app: dict, force: bool = False) -> dict:
    """Build the ranked URL list for one app and cache it."""
    out_path = DISCOVER_CACHE / f"{app['id']}.json"
    cached = read_json(out_path)
    # A hint-only result (made before the Tavily key existed) is redone once
    # search is available, instead of being trusted forever from the cache.
    stale = cached is not None and not cached.get("searched") and bool(SETTINGS.tavily_api_key)
    if cached is not None and not force and not stale:
        return cached

    domains = official_domains(app)
    seeds = hint_urls(app["hint"])
    candidates: dict[str, dict] = {}  # url -> {url, score, source}

    def official(url: str) -> bool:
        return is_official(url, app, domains)

    def add(url: str, source: str, search_score: float = 0.0) -> None:
        url = normalize_url(url)
        if SKIP_EXTENSIONS.search(urlparse(url).path):
            return  # images/archives: no text to read
        s = score_url(url, app, domains, search_score)
        if url not in candidates or s > candidates[url]["score"]:
            candidates[url] = {"url": url, "score": round(s, 2), "source": source}

    for seed in seeds:
        add(seed, "hint")
        for link in hint_page_links(seed, app, domains):
            add(link, "hint_page_link")

    best_per_query = []
    search_log = []
    if SETTINGS.tavily_api_key:
        for template in QUERIES:
            query = template.format(app=app["app"])
            try:
                results = composio_client.search(query)
            except Exception as exc:  # one failed query must not lose the app
                log.warning("search failed %r: %s", query, exc)
                search_log.append({"query": query, "error": str(exc)[:200]})
                continue
            search_log.append({"query": query, "urls": [r.url for r in results]})
            for r in results:
                add(r.url, f"search: {query}", r.score)
            ranked = [c for c in sorted((candidates[normalize_url(r.url)] for r in results),
                                        key=lambda c: -c["score"]) if c["score"] > 0]
            # Third-party pages are only trusted for the MCP query (community
            # MCP servers live on other sites by nature) or when the app has
            # no official site at all. Otherwise a same-name product (Sherlock
            # -> a Stanford cluster) would be researched instead of the app.
            allow_third_party = "MCP" in template or not domains
            pick = next((c for c in ranked if allow_third_party or official(c["url"])), None)
            if pick:
                best_per_query.append(pick["url"])
    else:
        log.warning("TAVILY_API_KEY not set: discovery for %s uses the hint only", app["app"])

    # Selection: hint seeds first (official by definition), then the best hit
    # of each query (so pricing/partner and MCP evidence always get a slot),
    # then spare slots from official pages only, by score.
    by_score = [c["url"] for c in sorted(candidates.values(), key=lambda c: -c["score"])
                if c["score"] > 0 and (official(c["url"]) or not domains)]
    chosen = unique([*seeds, *best_per_query, *by_score])[:MAX_URLS]

    result = {
        "id": app["id"], "app": app["app"], "official_domains": sorted(domains),
        "searched": bool(SETTINGS.tavily_api_key),
        "urls": [candidates[u] for u in chosen],
        "all_candidates": sorted(candidates.values(), key=lambda c: -c["score"])[:30],
        "searches": search_log,
    }
    write_json(out_path, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Step 1: discover docs URLs")
    add_app_selection_args(parser)
    args = parser.parse_args()
    setup_logging()
    for app in tqdm(select_apps(args), desc="discover"):
        res = discover_app(app, args.force)
        tqdm.write(f"{app['id']:>3} {app['app']:<26} {len(res['urls'])} urls")


if __name__ == "__main__":
    main()
