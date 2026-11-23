"""Step 2: fetch pages, clean them to text, cache them, and build each app's
trimmed docs bundle. No LLM.

Order of attempts for one URL:
  1. httpx + trafilatura. Free and unlimited, and it gives a real HTTP status
     code, which the evidence loop (Phase 4) needs ("did the cited page load?").
  2. Tavily extract via Composio, only for pages that came back thin (JS-only
     shells, bot walls). It renders pages but costs credits, so it's the fallback.
  3. Pages still thin are marked needs_browser for the Playwright loop (Phase 4).

Usage:
    python -m agent.fetch --all        # fetch every app's discovered URLs
    python -m agent.fetch --app Linear
"""
from __future__ import annotations

import argparse
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urljoin, urldefrag, urlparse

import httpx
import tiktoken
import trafilatura
from tqdm import tqdm

from agent import composio_client
from agent.common import add_app_selection_args, read_json, select_apps, sha1, unique, write_json
from agent.config import DISCOVER_CACHE, DOCS_CACHE, PAGES_CACHE, SETTINGS, setup_logging

log = logging.getLogger("agent.fetch")
# trafilatura logs "empty HTML tree" for every non-article page; we record
# failures in the page meta instead, so silence it.
logging.getLogger("trafilatura").setLevel(logging.CRITICAL)

MIN_GOOD_CHARS = 500          # below this a page is probably a JS shell or error page
MAX_TEXT_CHARS = 200_000      # huge API references: keep the start, it's enough
# Short pages containing these are walls, not docs.
WALL_MARKERS = re.compile(
    r"enable javascript|you need to enable|checking your browser|just a moment|"
    r"access denied|verify you are human|sign in to continue|log in to continue|captcha",
    re.I)
HEADERS = {
    # A normal browser UA: many docs sites serve bots an empty shell.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def normalize_url(url: str) -> str:
    """Drop #fragments and trailing slashes so the same page isn't fetched twice."""
    url = urldefrag(url.strip())[0]
    return url[:-1] if url.endswith("/") and urlparse(url).path not in ("", "/") else url


def page_paths(url: str) -> tuple:
    key = sha1(normalize_url(url))
    return PAGES_CACHE / f"{key}.json", PAGES_CACHE / f"{key}.txt"


def load_page(url: str) -> dict | None:
    """Cached page: meta dict with the text under 'text', or None."""
    meta_path, text_path = page_paths(url)
    meta = read_json(meta_path)
    if meta is None:
        return None
    meta["text"] = text_path.read_text(encoding="utf-8") if text_path.exists() else ""
    return meta


def _save_page(meta: dict, text: str) -> dict:
    meta_path, text_path = page_paths(meta["url"])
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(text, encoding="utf-8")
    write_json(meta_path, meta)
    return {**meta, "text": text}


def _links(html: str, base_url: str) -> list[str]:
    """Absolute http(s) links on the page. Discovery uses these to find the
    auth/pricing/partner pages next to the hint page."""
    hrefs = re.findall(r'href=["\']([^"\'#][^"\']*)["\']', html, re.I)
    absolute = (normalize_url(urljoin(base_url, h)) for h in hrefs)
    return unique(u for u in absolute if u.startswith("http"))[:300]


def is_thin(text: str) -> bool:
    """True if the text looks like a JS shell, bot wall or error page."""
    return len(text) < MIN_GOOD_CHARS or (len(text) < 2000 and bool(WALL_MARKERS.search(text)))


def fetch_page(url: str, force: bool = False) -> dict:
    """Fetch one URL with httpx + trafilatura, cached. Never raises: failures
    are recorded in the page meta so later steps can see why a page is missing."""
    url = normalize_url(url)
    if not force and (cached := load_page(url)) is not None:
        return cached

    meta = {"url": url, "final_url": url, "status": None, "method": "httpx",
            "content_type": "", "links": [], "error": None,
            "fetched_at": datetime.now(timezone.utc).isoformat()}
    text = ""
    try:
        resp = httpx.get(url, headers=HEADERS, follow_redirects=True, timeout=25)
        meta.update(status=resp.status_code, final_url=str(resp.url),
                    content_type=resp.headers.get("content-type", ""))
        if "html" in meta["content_type"] or meta["content_type"].startswith("text/"):
            html = resp.text
            meta["links"] = _links(html, str(resp.url))
            # favor_recall: docs pages are list/table heavy; precision mode drops too much.
            text = trafilatura.extract(html, include_tables=True, favor_recall=True) or ""
        else:
            meta["error"] = f"not html: {meta['content_type']}"
    except httpx.HTTPError as exc:
        meta["error"] = f"{type(exc).__name__}: {exc}"[:300]

    text = text[:MAX_TEXT_CHARS]
    meta.update(chars=len(text), ok=meta["status"] == 200 and not is_thin(text),
                needs_browser=is_thin(text))
    return _save_page(meta, text)


def rescue_with_composio(pages: list[dict]) -> list[dict]:
    """Retry thin pages with Tavily extract (via Composio), which renders JS.
    One call per app for all its thin pages. Failures are logged, not raised:
    the page simply stays needs_browser for the Playwright loop."""
    thin = [p for p in pages if p["needs_browser"] and p.get("method") == "httpx"]
    if not thin or not SETTINGS.tavily_api_key:
        return pages
    try:
        texts = composio_client.extract([p["url"] for p in thin])
    except Exception as exc:
        log.warning("Composio extract fallback failed for %d urls: %s", len(thin), exc)
        return pages

    rescued = {}
    for p in thin:
        text = (texts.get(p["url"]) or texts.get(p["final_url"]) or "")[:MAX_TEXT_CHARS]
        meta = {k: v for k, v in p.items() if k != "text"}
        # Keep the httpx status: it's the honest answer to "does this URL load?"
        meta.update(method="composio_tavily_extract", chars=len(text),
                    needs_browser=is_thin(text), ok=not is_thin(text))
        rescued[p["url"]] = _save_page(meta, text)
        log.info("Composio extract fallback %s -> %d chars", p["url"], len(text))
    return [rescued.get(p["url"], p) for p in pages]


# --- Trimmed docs bundle ------------------------------------------------------

# Paragraphs that mention these are the ones the extractor needs most.
KEYWORDS = re.compile(
    r"oauth|authori[sz]|authenticat|api[ _-]?key|token|client[ _]id|secret|bearer|basic auth|"
    r"pricing|price|plan|free|trial|paid|enterprise|partner|approv|review|apply|application|"
    r"contact (?:sales|us)|request access|rate[ -]?limit|quota|mcp|model context protocol|"
    r"endpoint|rest|graphql|webhook|sandbox|test mode|scope|developer",
    re.I)
_ENC = tiktoken.get_encoding("o200k_base")


def count_tokens(text: str) -> int:
    """Approximate token count (o200k_base). Close enough to keep a margin."""
    return len(_ENC.encode(text, disallowed_special=()))


def _chunks(text: str, max_chars: int = 700) -> list[str]:
    """Split page text into paragraph-sized chunks. Long paragraphs are split
    on sentence ends, so one giant block can't eat the whole budget."""
    out = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        while len(para) > max_chars:
            cut = para.rfind(". ", 0, max_chars)
            cut = cut + 1 if cut > max_chars // 3 else max_chars
            out.append(para[:cut].strip())
            para = para[cut:].strip()
        if len(para) > 30:
            out.append(para)
    return out


def build_docs_bundle(app: dict, pages: list[dict], path=None) -> dict:
    """Choose the highest-signal paragraphs across an app's good pages, within
    DOCS_TOKEN_BUDGET (minus a 10% margin). Each page's best paragraph is
    picked first, so every good page with any keyword hit is represented
    before any single page gets a second paragraph."""
    budget = int(SETTINGS.docs_token_budget * 0.9)
    good = [p for p in pages if p.get("ok")]

    candidates = []  # (score, page_index, chunk_index, text)
    for pi, page in enumerate(good):
        for ci, chunk in enumerate(_chunks(page["text"])):
            hits = len(KEYWORDS.findall(chunk))
            # Hits per 100 chars: rewards dense paragraphs over long ones.
            candidates.append((hits / max(len(chunk), 100) * 100, pi, ci, chunk))

    best_per_page = {}
    for cand in sorted(candidates, reverse=True):
        best_per_page.setdefault(cand[1], cand)
    ordered = list(best_per_page.values()) + sorted(candidates, reverse=True)

    chosen, used = {}, 0
    for score, pi, ci, chunk in ordered:
        if (pi, ci) in chosen or score == 0:
            continue
        cost = count_tokens(chunk) + 8  # +8 for the per-chunk separator
        if used + cost > budget:
            continue
        chosen[(pi, ci)] = chunk
        used += cost

    sections = []
    for pi, page in enumerate(good):
        parts = [chosen[k] for k in sorted(chosen) if k[0] == pi]
        if parts:
            sections.append({"url": page["url"], "text": "\n\n".join(parts)})
    bundle = {"id": app["id"], "app": app["app"], "tokens": used, "budget": budget,
              "sections": sections}
    write_json(path or DOCS_CACHE / f"{app['id']}.json", bundle)
    return bundle


# --- Per-app driver -------------------------------------------------------------

def fetch_app(app: dict, force: bool = False) -> list[dict]:
    """Fetch every discovered URL for one app, rescue thin ones, build the bundle."""
    discovered = read_json(DISCOVER_CACHE / f"{app['id']}.json")
    if discovered is None:
        raise SystemExit(f"Run discovery first: no cache/discover/{app['id']}.json")
    urls = [u["url"] for u in discovered["urls"]]
    with ThreadPoolExecutor(max_workers=6) as pool:
        pages = list(pool.map(lambda u: fetch_page(u, force), urls))
    pages = rescue_with_composio(pages)
    build_docs_bundle(app, pages)
    return pages


def main() -> None:
    parser = argparse.ArgumentParser(description="Step 2: fetch + clean + cache pages")
    add_app_selection_args(parser)
    args = parser.parse_args()
    setup_logging()
    for app in tqdm(select_apps(args), desc="fetch"):
        pages = fetch_app(app, args.force)
        ok = sum(p["ok"] for p in pages)
        tqdm.write(f"{app['id']:>3} {app['app']:<26} {ok}/{len(pages)} good pages")


if __name__ == "__main__":
    main()
