"""One place for every LLM call: model fallback chains, rate limiting,
retries, and a disk cache.

    chat(models, messages, ...) -> LLMResult

Rules, in the order a call goes through them:
  1. Cache: identical (provider, model, messages, options) -> stored answer.
     Re-runs cost zero quota. Only successful answers are cached.
  2. Models are tried in chain order. A model that hit its DAILY cap is
     skipped for the rest of the day (remembered on disk), and the next
     model in the chain answers instead.
  3. Before each request a per-model limiter waits until the request fits
     the free-tier per-minute limits (requests and tokens).
  4. HTTP 429 (per-minute) and 503 (overloaded) are retried with
     exponential backoff, honouring retry-after. Other errors raise.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

from agent.common import read_json, sha1, write_json
from agent.config import CACHE_DIR, SETTINGS, Limits

log = logging.getLogger("agent.llm")

LLM_CACHE = CACHE_DIR / "llm"
USAGE_FILE = CACHE_DIR / "llm_usage.json"   # per-day: calls/tokens per model, exhausted models


class DailyLimitError(Exception):
    """A model's daily quota is used up. Not retried: the chain moves on."""


class AllModelsExhausted(Exception):
    """Every model in the chain is out for today. Re-run tomorrow (resumable)."""


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    cached: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0


# --- Usage tracking (survives restarts, resets each day) ----------------------

_usage_lock = threading.Lock()


def _usage() -> dict:
    data = read_json(USAGE_FILE) or {}
    if data.get("date") != date.today().isoformat():
        data = {"date": date.today().isoformat(), "models": {}, "exhausted": []}
    return data


def _record(model: str, prompt_tokens: int, completion_tokens: int, cached: bool) -> None:
    with _usage_lock:
        data = _usage()
        m = data["models"].setdefault(model, {"calls": 0, "cache_hits": 0, "tokens": 0})
        if cached:
            m["cache_hits"] += 1
        else:
            m["calls"] += 1
            m["tokens"] += prompt_tokens + completion_tokens
        write_json(USAGE_FILE, data)


def _mark_exhausted(model: str) -> None:
    with _usage_lock:
        data = _usage()
        if model not in data["exhausted"]:
            data["exhausted"].append(model)
        write_json(USAGE_FILE, data)
    log.warning("%s hit its daily limit; skipping it until tomorrow", model)


def is_exhausted(model: str) -> bool:
    return model in _usage()["exhausted"]


def usage_today() -> dict:
    """For the run summary: calls, cache hits and tokens per model today."""
    return _usage()


# --- Per-model rate limiter -------------------------------------------------

class MinuteLimiter:
    """Sliding 60-second window of (time, tokens). wait() blocks until one
    more request of `tokens` fits under both the RPM and TPM limits."""

    def __init__(self, limits: Limits):
        self.limits = limits
        self.events: deque[tuple[float, int]] = deque()
        self.lock = threading.Lock()

    def wait(self, tokens: int) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                while self.events and now - self.events[0][0] > 60:
                    self.events.popleft()
                used = sum(t for _, t in self.events)
                rpm_ok = not self.limits.rpm or len(self.events) < self.limits.rpm
                tpm_ok = not self.limits.tpm or used + tokens <= self.limits.tpm or not self.events
                if rpm_ok and tpm_ok:
                    self.events.append((now, tokens))
                    return
                sleep_for = 60 - (now - self.events[0][0]) + 0.5
            time.sleep(max(sleep_for, 0.5))


_limiters: dict[str, MinuteLimiter] = {}


def _limiter(provider: str, model: str) -> MinuteLimiter:
    if model not in _limiters:
        limits = SETTINGS.groq_limits if provider == "groq" else SETTINGS.gemini_limits
        _limiters[model] = MinuteLimiter(limits)
    return _limiters[model]


# --- Provider calls -----------------------------------------------------------

def provider_of(model: str) -> str:
    return "gemini" if model.startswith("gemini") else "groq"


@lru_cache(maxsize=1)
def _groq():
    from groq import Groq
    # max_retries=0: we do our own retries, so the SDK must not hide 429s.
    return Groq(api_key=SETTINGS.groq_api_key, max_retries=0)


@lru_cache(maxsize=1)
def _gemini():
    from google import genai
    return genai.Client(api_key=SETTINGS.gemini_api_key)


def _reasoning_options(model: str) -> dict:
    """Reasoning tokens count against Groq's tight per-minute token cap, so
    ask for as little thinking as each model allows (probed in Phase 3)."""
    if model.startswith("qwen/"):
        return {"reasoning_effort": "none"}
    if model.startswith("openai/gpt-oss"):
        return {"reasoning_effort": "low"}
    return {}


def _call_groq(model: str, messages: list[dict], json_mode: bool, max_tokens: int) -> LLMResult:
    kwargs = {"model": model, "messages": messages, "max_tokens": max_tokens,
              "temperature": 0, **_reasoning_options(model)}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = _groq().chat.completions.create(**kwargs)
    usage = resp.usage
    return LLMResult(text=resp.choices[0].message.content or "", provider="groq", model=model,
                     cached=False, prompt_tokens=usage.prompt_tokens,
                     completion_tokens=usage.completion_tokens)


def _call_gemini(model: str, messages: list[dict], json_mode: bool, max_tokens: int) -> LLMResult:
    from google.genai import types
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    user = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
    config = types.GenerateContentConfig(
        system_instruction=system or None, temperature=0, max_output_tokens=max_tokens,
        response_mime_type="application/json" if json_mode else None)
    resp = _gemini().models.generate_content(model=model, contents=user, config=config)
    meta = resp.usage_metadata
    return LLMResult(text=resp.text or "", provider="gemini", model=model, cached=False,
                     prompt_tokens=getattr(meta, "prompt_token_count", 0) or 0,
                     completion_tokens=getattr(meta, "candidates_token_count", 0) or 0)


def _classify_error(exc: Exception) -> tuple[str, float | None]:
    """-> ('daily' | 'retry' | 'fatal', seconds to wait if the API said so)."""
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    text = str(exc).lower()
    retry_after = None
    response = getattr(exc, "response", None)
    if response is not None and response.headers.get("retry-after"):
        try:
            retry_after = float(response.headers["retry-after"])
        except ValueError:
            pass
    if retry_after is None and (m := re.search(r"try again in ([\d.]+)s", text)):
        retry_after = float(m.group(1))
    if status == 429 or "rate limit" in text or "resource_exhausted" in text:
        # Groq says "tokens per day (TPD)" / "requests per day (RPD)"; Gemini's
        # free-tier daily quota mentions "PerDay".
        if re.search(r"per day|\(tpd\)|\(rpd\)|perday", text):
            return "daily", retry_after
        return "retry", retry_after
    if status in (500, 502, 503, 504) or "unavailable" in text or "overloaded" in text:
        return "retry", retry_after
    return "fatal", None


def _call_with_retries(model: str, messages: list[dict], json_mode: bool,
                       max_tokens: int, est_tokens: int) -> LLMResult:
    provider = provider_of(model)
    call = _call_groq if provider == "groq" else _call_gemini
    delay = 5.0
    for attempt in range(SETTINGS.max_retries + 1):
        _limiter(provider, model).wait(est_tokens)
        try:
            return call(model, messages, json_mode, max_tokens)
        except Exception as exc:
            kind, retry_after = _classify_error(exc)
            if kind == "daily":
                raise DailyLimitError(str(exc)[:300]) from exc
            if kind == "fatal" or attempt == SETTINGS.max_retries:
                raise
            wait = retry_after if retry_after is not None else delay
            log.info("%s: %s, retry %d in %.0fs", model, type(exc).__name__, attempt + 1, wait)
            time.sleep(wait)
            delay *= 3  # 5s, 15s, 45s, ...
    raise RuntimeError("unreachable")


# --- Public entry point ---------------------------------------------------------

def chat(models: tuple[str, ...] | list[str], messages: list[dict], *, json_mode: bool = True,
         max_tokens: int = 1500, est_prompt_tokens: int = 0) -> LLMResult:
    """Ask the first available model in `models`; fall through the chain on
    daily caps (and on hard errors, so one broken model can't stop a run).

    est_prompt_tokens: the caller's token count of the prompt, used by the
    per-minute limiter (input + max output is what Groq counts).
    """
    def cache_file(model: str):
        key = sha1(json.dumps([provider_of(model), model, messages, json_mode, max_tokens],
                              sort_keys=True))
        return LLM_CACHE / f"{key}.json"

    # Check the cache for EVERY model first: if a fallback model answered this
    # exact prompt before, a re-run must reuse that, not spend the first
    # model's quota on it again.
    for model in models:
        if (cached := read_json(cache_file(model))) is not None:
            _record(model, 0, 0, cached=True)
            return LLMResult(**{**cached, "cached": True})

    errors = []
    for model in models:
        if is_exhausted(model):
            errors.append(f"{model}: exhausted today")
            continue
        try:
            result = _call_with_retries(model, messages, json_mode, max_tokens,
                                        est_prompt_tokens + max_tokens)
        except DailyLimitError as exc:
            _mark_exhausted(model)
            errors.append(f"{model}: daily limit ({exc})")
            continue
        except Exception as exc:
            log.warning("%s failed: %s", model, exc)
            errors.append(f"{model}: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        _record(model, result.prompt_tokens, result.completion_tokens, cached=False)
        write_json(cache_file(model), {k: v for k, v in result.__dict__.items() if k != "cached"})
        return result
    if errors and all("daily limit" in e or "exhausted" in e for e in errors):
        raise AllModelsExhausted("; ".join(errors))
    raise RuntimeError("all models failed: " + " | ".join(errors))
