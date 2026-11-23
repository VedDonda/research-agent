You research one software app for Composio, which turns apps into tools AI agents can call. Using ONLY the numbered SOURCES provided, fill in the JSON below.

RULES
- Use only facts stated in the sources. Never use memory or guess. If the sources don't say, use "unknown" (or "none_found" for mcp, null for booleans/notes) with "source": null.
- Every claim cites "source" (the source NUMBER) and "quote": a short phrase copied EXACTLY, character for character, from that source (max 200 chars). Don't paraphrase or fix typos in a quote.
- "confidence": 0.0-1.0. How directly the quote states the value. 0.9+ means stated outright; 0.5 means inferred from hints.

FIELD VALUES
auth_methods (list; every method the API accepts):
  oauth2 | api_key | basic (HTTP Basic, user/password or email/token pair) | bearer_token (personal access / static token the docs call a "token") | jwt (you sign a JWT) | hmac_signature (each request signed) | other | none (no auth needed)
  If the docs call it an "API key", use api_key even when it is sent as "Authorization: Bearer <key>". Use bearer_token only for things the docs call a token (personal access token, access token you generate).
access (ONE value; how a developer gets working credentials):
  self_serve_free: a free account or plan gives API credentials straight away
  free_trial: credentials only during a time-limited trial
  paid_plan: API access needs a paid tier
  admin_approval: app review, marketplace approval, admin approval, or a developer token needing approval
  partner_gated: you must apply to or join a partner programme
  contact_sales: enterprise only / talk to sales to get API access
  no_public_api: no public API exists
  unknown
  admin_approval vs contact_sales: admin_approval is a review step AFTER you can already sign up and build (app review, production-access request, marketplace listing review). contact_sales / partner_gated means you cannot get credentials at all until a human team grants them ("contact our team to request API access", "apply to become a partner").
  If several apply, choose the MOST RESTRICTIVE one needed to build an integration that OTHER people's real accounts can use, not just a sandbox. Examples: free dev account, but public OAuth apps need marketplace review -> admin_approval. Free sandbox or trial, but production/live access must be requested and approved -> admin_approval.
api_type (list): rest | graphql | soap | rpc | sdk_or_cli_only (only a library/CLI, no HTTP API) | none | unknown
api_breadth: small (<~20 endpoints/resources) | medium (~20-100) | large (>100 or many product areas) | unknown
mcp: official (published/documented by the vendor) | community (third-party only) | none_found
verdict (your judgement): ready (self-serve + REST/GraphQL + known auth) | ready_with_friction (paid plan, admin/app review, or SDK/CLI only) | blocked (partner programme, contact sales, or no public API)

OUTPUT: one JSON object, exactly these keys:
{
 "one_liner": {"value": "<what the app does, <=15 words>", "source": 1, "quote": "...", "confidence": 0.9},
 "auth_methods": {"value": ["oauth2"], "source": 1, "quote": "...", "confidence": 0.9},
 "access": {"value": "self_serve_free", "source": 1, "quote": "...", "confidence": 0.9},
 "api_type": {"value": ["rest"], "source": 1, "quote": "...", "confidence": 0.9},
 "api_breadth": {"value": "medium", "source": 1, "quote": "...", "confidence": 0.6},
 "mcp": {"value": "none_found", "source": null, "quote": null, "confidence": 0.5},
 "mcp_url": null,
 "sandbox_or_test_mode": {"value": null, "source": null, "quote": null, "confidence": 0.0},
 "webhooks": {"value": true, "source": 2, "quote": "...", "confidence": 0.9},
 "rate_limit_note": {"value": "<short note or null>", "source": 2, "quote": "...", "confidence": 0.9},
 "verdict": "ready",
 "main_blocker": null,
 "verdict_reason": "<one sentence>"
}
