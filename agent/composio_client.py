"""Everything that talks to Composio, behind three small functions:

    search(query)            -> list[SearchResult]   web search (Tavily, via Composio)
    extract(urls)            -> dict[url, text]      page text (Tavily extract, via Composio)
    list_composio_toolkits() -> list[dict]           Composio's own toolkit catalogue

Why Tavily *through* Composio: Composio's free built-in search toolkit
(COMPOSIO_SEARCH) is retired (HTTP 410 on every version), and Composio's
"Instant" accounts are a paid project setting. So we connect our own free
Tavily key to Composio once, as a connected account, and call TAVILY_SEARCH
through the Composio SDK. See docs/composio_friction.md for what we hit.

Every call is cached on disk, so re-runs cost no Tavily credits.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from functools import lru_cache

from composio import Composio
from composio.core.models.connected_accounts import auth_scheme

from agent.common import read_json, sha1, write_json
from agent.config import CACHE_DIR, SEARCH_CACHE, SETTINGS

log = logging.getLogger("agent.composio")

TAVILY = "tavily"
CATALOGUE_CACHE = CACHE_DIR / "composio_toolkits.json"


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str      # Tavily's short content excerpt for the result
    score: float      # Tavily's relevance score (0-1)


@lru_cache(maxsize=1)
def _client() -> Composio:
    return Composio(api_key=SETTINGS.composio_api_key)


# --- Connecting the Tavily key (one-time setup, then reused) -----------------

@lru_cache(maxsize=1)
def tavily_account_id() -> str:
    """Return the id of an ACTIVE Tavily connected account for our user,
    creating the auth config + connection the first time.

    Why look up first: creating a new connection on every run would pile up
    duplicate accounts in the Composio dashboard.
    """
    c, user = _client(), SETTINGS.composio_user_id
    existing = c.connected_accounts.list(
        toolkit_slugs=[TAVILY], user_ids=[user], statuses=["ACTIVE"])
    if existing.items:
        return existing.items[0].id

    if not SETTINGS.tavily_api_key:
        raise RuntimeError("TAVILY_API_KEY is not set in .env (free key: app.tavily.com)")

    # Reuse our own Tavily auth config if one exists, else make one.
    configs = c.auth_configs.list(toolkit_slug=TAVILY, is_composio_managed=False)
    if configs.items:
        auth_config_id = configs.items[0].id
    else:
        auth_config_id = c.auth_configs.create(
            toolkit=TAVILY, options={"type": "use_custom_auth", "auth_scheme": "API_KEY"}).id

    request = c.connected_accounts.initiate(
        user_id=user,
        auth_config_id=auth_config_id,
        config=auth_scheme.api_key({"generic_api_key": SETTINGS.tavily_api_key}),
    )
    log.info("Created Tavily connected account %s", request.id)
    return request.id


def _execute(slug: str, arguments: dict) -> dict:
    """Run one Composio tool on our Tavily account and return its `data`.

    dangerously_skip_version_check: direct execution otherwise demands a pinned
    toolkit version. Using the latest is fine here; if Composio retires it
    (as happened to COMPOSIO_SEARCH) the call fails loudly with HTTP 410.
    """
    result = _client().tools.execute(
        slug,
        arguments=arguments,
        user_id=SETTINGS.composio_user_id,
        connected_account_id=tavily_account_id(),
        dangerously_skip_version_check=True,
    )
    data = result if isinstance(result, dict) else result.model_dump()
    if not data.get("successful", True):
        raise RuntimeError(f"{slug} failed: {data.get('error')}")
    return data.get("data") or {}


# --- 1. Search ----------------------------------------------------------------

def search(query: str, max_results: int = 5,
           include_domains: list[str] | None = None) -> list[SearchResult]:
    """Web search via Composio -> Tavily. Cached by the exact query + options."""
    key = sha1(f"tavily|{query}|{max_results}|{sorted(include_domains or [])}")
    cache_file = SEARCH_CACHE / f"{key}.json"
    cached = read_json(cache_file)
    if cached is not None:
        return [SearchResult(**r) for r in cached["results"]]

    args: dict = {"query": query, "max_results": max_results, "search_depth": "basic"}
    if include_domains:
        args["include_domains"] = include_domains
    data = _execute("TAVILY_SEARCH", args)

    # The response nests results differently across versions; accept both.
    raw = data.get("results") or data.get("response_data", {}).get("results") or []
    results = [
        SearchResult(url=r.get("url", ""), title=r.get("title", ""),
                     snippet=(r.get("content") or "")[:500], score=float(r.get("score") or 0))
        for r in raw if r.get("url")
    ]
    write_json(cache_file, {"query": query, "args": args, "results": [asdict(r) for r in results]})
    return results


# --- 2. Extract (fallback scraper) --------------------------------------------

def extract(urls: list[str]) -> dict[str, str]:
    """Page text via Composio -> Tavily extract. Used only when plain httpx
    gets a thin page (JS shell, bot wall), because each call costs credits.
    Not cached here: fetch.py caches the resulting page text itself."""
    data = _execute("TAVILY_EXTRACT", {"urls": urls, "extract_depth": "advanced", "format": "markdown"})
    raw = data.get("results") or data.get("response_data", {}).get("results") or []
    return {r["url"]: r.get("raw_content") or "" for r in raw if r.get("url")}


# --- 3. Composio's toolkit catalogue ------------------------------------------

def list_composio_toolkits(refresh: bool = False) -> list[dict]:
    """Every toolkit in Composio's catalogue as {slug, name}.

    Pages through the list with a cursor: a single call returns at most 1,000
    of the ~1,865 toolkits, which would silently make many apps look "not
    built yet". Cached to disk; pass refresh=True to re-download.
    """
    cached = read_json(CATALOGUE_CACHE)
    if cached is not None and not refresh:
        return cached

    items, cursor = [], None
    while True:
        params = {"limit": 1000, **({"cursor": cursor} if cursor else {})}
        page = _client().client.toolkits.list(**params)
        items += [{"slug": t.slug, "name": t.name} for t in page.items]
        cursor = getattr(page, "next_cursor", None)
        if not cursor or not page.items:
            break
    write_json(CATALOGUE_CACHE, items)
    log.info("Composio catalogue: %d toolkits", len(items))
    return items


def _norm(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def match_toolkits(app_name: str, catalogue: list[dict]) -> dict:
    """Does Composio already have a toolkit for this app?

    exact: toolkits whose slug or name equals one of the app's name forms
           ('Lark (Larksuite)' -> 'larklarksuite', 'lark', 'larksuite').
           This decides composio_toolkit_exists.
    near:  toolkits that merely start with the app's first word (e.g.
           'WhatsApp Business' -> 'whatsapp'). Not counted as a match; listed
           so a human can confirm, because name-prefix matching produces false
           positives ('Google Ads' would match every google_* toolkit).
    """
    inner = [p.strip() for p in app_name.replace(")", "(").split("(") if p.strip()]
    # Also try without a domain suffix: 'Monday.com' -> 'monday', 'systeme.io' -> 'systeme'.
    no_tld = [re.sub(r"\.(com|io|ai|app|dev)\b", "", p, flags=re.I) for p in [app_name, *inner]]
    forms = {_norm(app_name), *(_norm(p) for p in inner), *(_norm(p) for p in no_tld)} - {""}
    first = _norm(app_name.split()[0])
    exact, near = [], []
    for tk in catalogue:
        # Composio ships some apps as MCP-backed toolkits ('pylon_mcp'):
        # those are toolkits for the app too, so compare without the suffix.
        slug = re.sub(r"mcp$", "", _norm(tk["slug"]))
        name = re.sub(r"mcp$", "", _norm(tk["name"]))
        if slug in forms or name in forms:
            exact.append(tk["slug"])
        elif len(first) >= 4 and (slug.startswith(first) or name.startswith(first)):
            near.append(tk["slug"])
    return {"exists": bool(exact), "exact": exact, "near": near[:8]}
