"""Central config: paths, API keys, model names and rate limits.

Why one module: every other file imports settings from here, so swapping a
model or a rate limit means editing .env only, never the code.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# --- Paths -----------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = ROOT / "cache"          # search results, pages, LLM responses (gitignored)
RESULTS_DIR = ROOT / "results"
EVAL_DIR = ROOT / "eval"
PROMPTS_DIR = ROOT / "prompts"
LOGS_DIR = ROOT / "logs"
APPS_CSV = DATA_DIR / "apps.csv"

# Cache sub-folders, one per pipeline step (all gitignored, all re-creatable)
SEARCH_CACHE = CACHE_DIR / "search"       # Tavily results, keyed by sha1(query)
DISCOVER_CACHE = CACHE_DIR / "discover"   # chosen URLs per app: {id}.json
PAGES_CACHE = CACHE_DIR / "pages"         # fetched pages: {sha1(url)}.txt + .json
DOCS_CACHE = CACHE_DIR / "docs"           # trimmed docs bundle per app: {id}.json
PRESCAN_CACHE = CACHE_DIR / "prescan"     # regex signals per app: {id}.json

# override=False: a real environment variable (e.g. a GitHub Actions secret)
# wins over the local .env file.
load_dotenv(ROOT / ".env", override=False)


def _int(name: str, default: int) -> int:
    """Read an int from the environment, falling back to a default."""
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _list(name: str, default: str) -> tuple[str, ...]:
    """Read a comma-separated model chain, e.g. "a,b" -> ("a", "b")."""
    raw = os.getenv(name, "").strip() or default
    return tuple(m.strip() for m in raw.split(",") if m.strip())


@dataclass(frozen=True)
class Limits:
    """Free-tier limits for ONE model. Groq and Gemini count quota per model,
    which is why the extractor can rotate models to pool daily quota."""
    rpm: int | None = None   # requests per minute
    rpd: int | None = None   # requests per day
    tpm: int | None = None   # tokens per minute (input + output)
    tpd: int | None = None   # tokens per day


@dataclass(frozen=True)
class Settings:
    # Keys
    composio_api_key: str
    composio_user_id: str   # whose connected accounts (e.g. Tavily) Composio uses
    tavily_api_key: str     # connected into Composio once; search runs through Composio
    groq_api_key: str
    gemini_api_key: str

    # Models by role. A tuple is a fallback chain, tried in order.
    extract_models: tuple[str, ...]          # Groq
    critic_model: str                        # Groq, must differ from the extractor
    gemini_models: tuple[str, ...]           # tie-breaker: primary, then fallback
    tiebreak_backup_models: tuple[str, ...]  # Groq, used if Gemini is down

    # Limits per model
    groq_limits: Limits
    gemini_limits: Limits

    # Token budgets. Groq's 8K tokens/min applies to a whole request
    # (input + output), so one extraction must fit well inside it.
    docs_token_budget: int    # trimmed docs per app
    request_token_cap: int    # prompt + max output for one call
    max_retries: int


def load_settings() -> Settings:
    """Build Settings from the environment. Missing keys become "" so the
    smoke test can report exactly which ones are missing instead of crashing."""
    return Settings(
        composio_api_key=os.getenv("COMPOSIO_API_KEY", ""),
        composio_user_id=os.getenv("COMPOSIO_USER_ID", "").strip() or "research-agent",
        tavily_api_key=os.getenv("TAVILY_API_KEY", "").strip(),
        groq_api_key=os.getenv("GROQ_API_KEY", ""),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        extract_models=_list("EXTRACT_MODELS", "qwen/qwen3.8-27b,openai/gpt-oss-20b"),
        critic_model=os.getenv("CRITIC_MODEL", "").strip() or "openai/gpt-oss-120b",
        gemini_models=(
            os.getenv("GEMINI_MODEL", "").strip() or "gemini-3.8-flash",
            os.getenv("GEMINI_FALLBACK_MODEL", "").strip() or "gemini-3.6-flash",
        ),
        tiebreak_backup_models=_list(
            "TIEBREAK_BACKUP_MODELS", "openai/gpt-oss-20b,qwen/qwen3.8-27b"),
        groq_limits=Limits(
            rpm=_int("GROQ_RPM", 30),
            rpd=_int("GROQ_RPD", 1000),
            tpm=_int("GROQ_TPM", 8000),
            tpd=_int("GROQ_TPD", 200_000),
        ),
        gemini_limits=Limits(
            rpm=_int("GEMINI_RPM", 5),
            rpd=_int("GEMINI_RPD", 18),  # free cap is ~20/day; keep a margin
        ),
        docs_token_budget=_int("DOCS_TOKEN_BUDGET", 3000),
        request_token_cap=_int("REQUEST_TOKEN_CAP", 6500),
        max_retries=_int("MAX_RETRIES", 5),
    )


def all_groq_models(s: Settings) -> list[str]:
    """Every distinct Groq model the config uses (for the smoke test)."""
    models = [*s.extract_models, s.critic_model, *s.tiebreak_backup_models]
    return list(dict.fromkeys(models))  # de-duplicate, keep order


SETTINGS = load_settings()


def setup_logging() -> logging.Logger:
    """Log to logs/run.log with timestamps. The console is left free for the
    tqdm progress bar and the final summary."""
    LOGS_DIR.mkdir(exist_ok=True)
    logger = logging.getLogger("agent")
    if logger.handlers:  # already configured (module imported twice)
        return logger
    logger.setLevel(logging.INFO)
    # Don't also print to the console (some SDKs attach root handlers):
    # the console is for progress bars and summaries.
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.FileHandler(LOGS_DIR / "run.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    return logger
