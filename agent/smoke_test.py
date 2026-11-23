"""Phase 0 smoke test: does every key and every configured model work?

Run:  python -m agent.smoke_test

Why: before spending hours on the pipeline, prove that every key, model name
and SDK import is valid. Each check is independent, so one bad key doesn't hide
the others. Keys are never printed.
"""
from __future__ import annotations

import sys
from typing import Callable

from agent.config import APPS_CSV, SETTINGS, all_groq_models
from agent.schema import Access, AppRecord, AuthMethod, Evidenced

PROMPT = "Reply with the single word: OK"


def check_schema() -> str:
    """Offline: the 100 apps load and a record round-trips through pydantic."""
    import pandas as pd

    apps = pd.read_csv(APPS_CSV)
    assert list(apps.columns) == ["id", "app", "category", "hint"], list(apps.columns)
    assert len(apps) == 100, f"expected 100 apps, got {len(apps)}"
    assert apps["id"].is_unique, "duplicate ids in apps.csv"
    assert apps["category"].nunique() == 10, "expected 10 categories"

    rec = AppRecord(
        id=1, app="Test", category="X",
        auth_methods=Evidenced[list[AuthMethod]](
            value=["oauth2"], evidence_url="https://x", snippet="a" * 400, confidence=0.9),
        access=Evidenced[Access](value="self_serve_free"),
        pass_=1,
    )
    data = rec.to_json_dict()
    assert data["pass"] == 1 and len(data["auth_methods"]["snippet"]) == 300
    assert AppRecord.model_validate(data) == rec  # JSON -> model -> same record
    return "100 apps, 10 categories, schema round-trips"


def check_tokens() -> str:
    """Offline: tiktoken loads (it downloads its encoding once, then caches)."""
    import tiktoken

    enc = tiktoken.get_encoding("o200k_base")
    n = len(enc.encode("OAuth 2.0 authorization code flow"))
    return f"o200k_base ok ({n} tokens in a test phrase); docs budget {SETTINGS.docs_token_budget}"


def check_composio() -> str:
    """One catalogue lookup. This also confirms we can later check
    'does Composio already have a toolkit for this app?'."""
    from composio import Composio

    client = Composio(api_key=SETTINGS.composio_api_key)
    toolkit = client.toolkits.get("github")
    return f"toolkit lookup ok: {getattr(toolkit, 'slug', toolkit)!s}"[:80]


def check_tavily() -> str:
    """Connect the Tavily key to Composio (first run only), then run one
    cached search THROUGH Composio. Costs 1 Tavily credit the first time."""
    from agent import composio_client

    account = composio_client.tavily_account_id()
    results = composio_client.search("Linear API authentication", max_results=3)
    return f"via Composio account {account[:12]}...: {len(results)} results, e.g. {results[0].url if results else '-'}"


def make_groq_check(model: str) -> Callable[[], str]:
    """One check per Groq model: each has its own quota and may be renamed."""
    def check() -> str:
        from groq import Groq

        client = Groq(api_key=SETTINGS.groq_api_key)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": PROMPT}],
            # Reasoning models think before answering; leave room for that.
            max_tokens=300,
        )
        return f"{model} -> {(resp.choices[0].message.content or '').strip()!r}"
    return check


def check_gemini() -> str:
    """Try the tie-breaker chain in order and report which model answered.
    Gemini being down is allowed (Groq backs it up), but we want to know.
    Costs 1 of the ~20 free daily requests per model tried."""
    from google import genai

    client = genai.Client(api_key=SETTINGS.gemini_api_key)
    errors = []
    for model in SETTINGS.gemini_models:
        try:
            resp = client.models.generate_content(model=model, contents=PROMPT)
            note = f" (after: {'; '.join(errors)})" if errors else ""
            return f"{model} -> {(resp.text or '').strip()!r}{note}"
        except Exception as exc:
            errors.append(f"{model}: {type(exc).__name__} {str(exc)[:80]}")
    raise RuntimeError(" | ".join(errors))


def main() -> int:
    # (name, function, the key it needs). None = no key needed; "" = key missing.
    checks: list[tuple[str, Callable[[], str], str | None]] = [
        ("schema", check_schema, None),
        ("tiktoken", check_tokens, None),
        ("composio", check_composio, SETTINGS.composio_api_key),
        ("tavily", check_tavily, SETTINGS.tavily_api_key),
    ]
    for model in all_groq_models(SETTINGS):
        checks.append(("groq", make_groq_check(model), SETTINGS.groq_api_key))
    checks.append(("gemini", check_gemini, SETTINGS.gemini_api_key))

    failures = 0
    for name, fn, key in checks:
        if key == "":
            print(f"[MISSING] {name:9} key not set in .env")
            failures += 1
            continue
        try:
            print(f"[OK]      {name:9} {fn()}")
        except Exception as exc:  # report every failure, keep going
            print(f"[FAIL]    {name:9} {type(exc).__name__}: {str(exc)[:300]}")
            failures += 1
    print("\nAll checks passed." if failures == 0 else f"\n{failures} check(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
