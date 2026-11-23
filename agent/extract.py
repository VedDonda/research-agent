"""Step 4: one LLM call per app turns its docs bundle into a schema record.

Design choices:
- The model cites a SOURCE NUMBER, not a URL. Models mangle long URLs;
  numbers are cheap in tokens and map back to the exact fetched URL in code.
- Quotes must be verbatim. They aren't trusted here: the evidence loop in
  Phase 4 checks each quote against the page text.
- The verdict stored on the record comes from rules.py, not the LLM. The
  LLM's own verdict is kept as llm_verdict so disagreement can be flagged.
- Validation by pydantic; one retry with the error message if it fails.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field, ValidationError, field_validator

from agent import llm
from agent.common import read_json
from agent.config import DOCS_CACHE, PROMPTS_DIR, RESULTS_DIR, SETTINGS
from agent.fetch import count_tokens
from agent.rules import rule_verdict
from agent.schema import (Access, ApiType, AppRecord, AuthMethod, Breadth, Evidenced,
                          McpStatus, Verdict)

log = logging.getLogger("agent.extract")

MAX_OUTPUT_TOKENS = 1400
SYSTEM_PROMPT = (PROMPTS_DIR / "extract.md").read_text(encoding="utf-8")
TOOLKIT_MATCH = RESULTS_DIR / "composio_toolkit_match.json"

T = TypeVar("T")


# --- What the LLM must return (validated before we trust it) -----------------

class Claim(BaseModel, Generic[T]):
    value: T
    source: int | None = None
    quote: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


def _lower(v: Any) -> Any:
    """Models sometimes answer 'OAuth2' or 'REST'; enums are lowercase."""
    if isinstance(v, str):
        return v.strip().lower()
    if isinstance(v, list):
        return [x.strip().lower() if isinstance(x, str) else x for x in v]
    return v


class ExtractionOutput(BaseModel):
    one_liner: Claim[str]
    auth_methods: Claim[list[AuthMethod]]
    access: Claim[Access]
    api_type: Claim[list[ApiType]]
    api_breadth: Claim[Breadth]
    mcp: Claim[McpStatus]
    mcp_url: str | None = None
    sandbox_or_test_mode: Claim[bool | None]
    webhooks: Claim[bool | None]
    rate_limit_note: Claim[str | None]
    verdict: Verdict
    main_blocker: str | None = None
    verdict_reason: str = ""

    @field_validator("auth_methods", "access", "api_type", "api_breadth", "mcp", mode="before")
    @classmethod
    def _normalise_enum_claims(cls, v: Any) -> Any:
        if isinstance(v, dict) and "value" in v:
            return {**v, "value": _lower(v["value"])}
        return v

    @field_validator("verdict", mode="before")
    @classmethod
    def _normalise_verdict(cls, v: Any) -> Any:
        return _lower(v)


# --- Prompt -------------------------------------------------------------------

def _user_message(app: dict, sections: list[dict]) -> str:
    sources = "\n\n".join(f"[SOURCE {i}] {s['url']}\n{s['text']}" for i, s in enumerate(sections, 1))
    return (f"APP: {app['app']}\nCATEGORY: {app['category']}\nHINT: {app['hint']}\n\n"
            f"SOURCES:\n{sources or '(no usable pages were fetched)'}\n\nReturn the JSON object.")


def _fit_to_cap(app: dict, sections: list[dict]) -> tuple[list[dict], int]:
    """Drop paragraphs from the longest source until prompt + max output fits
    REQUEST_TOKEN_CAP. Instructions and schema are never cut."""
    sections = [dict(s) for s in sections]
    while True:
        tokens = count_tokens(SYSTEM_PROMPT) + count_tokens(_user_message(app, sections))
        if tokens + MAX_OUTPUT_TOKENS <= SETTINGS.request_token_cap or not sections:
            return sections, tokens
        longest = max(sections, key=lambda s: len(s["text"]))
        paras = longest["text"].split("\n\n")
        if len(paras) > 1:
            longest["text"] = "\n\n".join(paras[:-1])
        else:
            sections.remove(longest)


# --- Converting the LLM answer to our record -----------------------------------

def _evidenced(claim: Claim, sections: list[dict]) -> Evidenced:
    """Source number -> the real URL. An out-of-range number gives no URL,
    which the evidence loop then flags."""
    url = None
    if claim.source and 1 <= claim.source <= len(sections):
        url = sections[claim.source - 1]["url"]
    return Evidenced(value=claim.value, evidence_url=url, snippet=claim.quote,
                     confidence=claim.confidence)


def _toolkit_exists(app_id: int) -> bool | None:
    match = (read_json(TOOLKIT_MATCH) or {}).get(str(app_id))
    return match["exists"] if match else None


def to_record(app: dict, out: ExtractionOutput, sections: list[dict], model: str) -> AppRecord:
    fields = {name: _evidenced(getattr(out, name), sections) for name in (
        "one_liner", "auth_methods", "access", "api_type", "api_breadth", "mcp",
        "sandbox_or_test_mode", "webhooks", "rate_limit_note")}
    verdict, reason = rule_verdict(out.access.value, out.api_type.value, out.auth_methods.value)
    confidences = [f.confidence for f in fields.values()]
    return AppRecord(
        id=app["id"], app=app["app"], category=app["category"], hint=app["hint"],
        **fields, mcp_url=out.mcp_url,
        composio_toolkit_exists=_toolkit_exists(app["id"]),
        verdict=verdict, llm_verdict=out.verdict, main_blocker=out.main_blocker,
        verdict_reason=out.verdict_reason or reason,
        confidence=round(sum(confidences) / len(confidences), 2),
        sources=[s["url"] for s in sections], extractor_model=model, pass_=1,
    )


# --- Entry point -------------------------------------------------------------------

def extract_app(app: dict) -> AppRecord:
    """One app: bundle -> prompt -> LLM -> validated record."""
    bundle = read_json(DOCS_CACHE / f"{app['id']}.json")
    if bundle is None:
        raise RuntimeError(f"no docs bundle for {app['app']}: run --prep first")
    sections, prompt_tokens = _fit_to_cap(app, bundle["sections"])
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_message(app, sections)}]

    for attempt in (1, 2):
        result = llm.chat(SETTINGS.extract_models, messages, json_mode=True,
                          max_tokens=MAX_OUTPUT_TOKENS, est_prompt_tokens=prompt_tokens)
        try:
            out = ExtractionOutput.model_validate(json.loads(result.text))
            return to_record(app, out, sections, result.model)
        except (json.JSONDecodeError, ValidationError) as exc:
            if attempt == 2:
                raise RuntimeError(f"invalid JSON from {result.model} after retry: {exc}") from exc
            log.info("%s: invalid output from %s, retrying with the error", app["app"], result.model)
            # Show the model its own answer and the exact error; that's the
            # cheapest way to get a corrected answer (one extra call).
            messages = messages + [
                {"role": "assistant", "content": result.text},
                {"role": "user", "content": f"That JSON failed validation:\n{str(exc)[:800]}\n"
                                            "Return the corrected JSON object only."},
            ]
            prompt_tokens += count_tokens(result.text) + 250
    raise RuntimeError("unreachable")
