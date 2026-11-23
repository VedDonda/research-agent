"""Data schema for one researched app.

Design choices:
- Every researched answer is an `Evidenced[T]`: the value plus the URL and a
  verbatim snippet that support it. That makes every claim checkable by code
  (verify.py) and by a human reviewer.
- Enums, not free text, so answers can be counted (analysis) and scored
  against ground truth (eval) without fuzzy matching.
- The class is called `Evidenced`, not `Field`, to avoid clashing with
  pydantic's own `Field`.
"""
from __future__ import annotations

from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

SNIPPET_MAX_CHARS = 300


# --- Enums -------------------------------------------------------------------

class AuthMethod(str, Enum):
    oauth2 = "oauth2"
    api_key = "api_key"
    basic = "basic"
    bearer_token = "bearer_token"      # personal access token / static token
    jwt = "jwt"
    hmac_signature = "hmac_signature"
    other = "other"
    none = "none"


class Access(str, Enum):
    """Ordered least to most restrictive: see ACCESS_RESTRICTIVENESS."""
    self_serve_free = "self_serve_free"   # a free account/plan gives API credentials
    free_trial = "free_trial"             # credentials on a time-limited trial
    paid_plan = "paid_plan"               # API needs a paid tier
    admin_approval = "admin_approval"     # app review / admin approval / dev-token approval
    partner_gated = "partner_gated"       # partner programme / application required
    contact_sales = "contact_sales"       # talk to sales / enterprise only
    no_public_api = "no_public_api"
    unknown = "unknown"


# Rule from the plan: if several access levels apply, record the most
# restrictive one needed to build a toolkit *other people* can use.
ACCESS_RESTRICTIVENESS: list[Access] = [
    Access.self_serve_free,
    Access.free_trial,
    Access.paid_plan,
    Access.admin_approval,
    Access.partner_gated,
    Access.contact_sales,
    Access.no_public_api,
]


class ApiType(str, Enum):
    rest = "rest"
    graphql = "graphql"
    soap = "soap"
    rpc = "rpc"
    sdk_or_cli_only = "sdk_or_cli_only"
    none = "none"
    unknown = "unknown"


class Breadth(str, Enum):
    small = "small"      # < ~20 endpoints/resources
    medium = "medium"    # ~20-100
    large = "large"      # > 100, or many product areas
    unknown = "unknown"


class McpStatus(str, Enum):
    official = "official"
    community = "community"
    none_found = "none_found"


class Verdict(str, Enum):
    ready = "ready"
    ready_with_friction = "ready_with_friction"
    blocked = "blocked"


# --- One evidenced answer ---------------------------------------------------

T = TypeVar("T")


class Evidenced(BaseModel, Generic[T]):
    """An answer plus the evidence behind it.

    evidence_url and snippet are optional because "unknown" answers have no
    evidence. verify.py flags any non-unknown value that is missing them.
    """
    value: T
    evidence_url: str | None = None
    snippet: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("snippet")
    @classmethod
    def _cap_snippet(cls, v: str | None) -> str | None:
        # Cut rather than reject: a prefix of a verbatim quote is still
        # verbatim, so the evidence check in verify.py still works on it.
        if v is not None and len(v) > SNIPPET_MAX_CHARS:
            return v[:SNIPPET_MAX_CHARS]
        return v


# --- The full record for one app --------------------------------------------

class AppRecord(BaseModel):
    """One row of results/pass1.jsonl, pass2.jsonl and final.json."""
    # populate_by_name: we can build it with pass_=1 in Python while the JSON
    # key is "pass" (a reserved word in Python).
    model_config = ConfigDict(populate_by_name=True)

    # Input (from data/apps.csv)
    id: int
    app: str
    category: str
    hint: str = ""

    # Researched fields (extractor fills these, verify.py checks them)
    one_liner: Evidenced[str] | None = None
    auth_methods: Evidenced[list[AuthMethod]] | None = None
    access: Evidenced[Access] | None = None
    api_type: Evidenced[list[ApiType]] | None = None
    api_breadth: Evidenced[Breadth] | None = None
    mcp: Evidenced[McpStatus] | None = None
    mcp_url: str | None = None
    sandbox_or_test_mode: Evidenced[bool | None] | None = None
    webhooks: Evidenced[bool | None] | None = None
    rate_limit_note: Evidenced[str | None] | None = None

    # From Composio's catalogue, not from the LLM
    composio_toolkit_exists: bool | None = None

    # Verdict: derived by rules in verify.py; the LLM's own guess is kept
    # separately so disagreement can be flagged.
    verdict: Verdict | None = None
    llm_verdict: Verdict | None = None
    main_blocker: str | None = None
    verdict_reason: str | None = None

    # Trust and provenance
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_human: bool = False
    human_note: str | None = None
    sources: list[str] = Field(default_factory=list)
    extractor_model: str | None = None   # which model in the chain answered
    pass_: int = Field(default=1, alias="pass")
    changed_fields: list[str] = Field(default_factory=list)

    def to_json_dict(self) -> dict:
        """Serialise with the public key names (e.g. "pass", not "pass_")."""
        return self.model_dump(mode="json", by_alias=True)
