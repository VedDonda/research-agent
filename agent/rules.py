"""The buildability verdict, decided by code from the researched fields.
The LLM also proposes a verdict; Phase 4 flags any
disagreement. Keeping this in code means the verdict is consistent across
all 100 apps and can be explained in one sentence.
"""
from __future__ import annotations

from agent.schema import Access, ApiType, AuthMethod, Verdict

BLOCKING_ACCESS = {Access.partner_gated, Access.contact_sales, Access.no_public_api}
FRICTION_ACCESS = {Access.paid_plan, Access.admin_approval}
OPEN_ACCESS = {Access.self_serve_free, Access.free_trial}
HTTP_APIS = {ApiType.rest, ApiType.graphql}


def rule_verdict(access: Access | None, api_types: list[ApiType] | None,
                 auth_methods: list[AuthMethod] | None) -> tuple[Verdict | None, str]:
    """-> (verdict, reason). None means the facts are too unknown to judge;
    that's flagged for re-research rather than guessed."""
    api = set(api_types or [])
    auth = set(auth_methods or [])

    # Blocked wins over everything: no amount of API breadth helps if you can't get in.
    if access in BLOCKING_ACCESS:
        return Verdict.blocked, f"access is {access.value}"
    if api == {ApiType.none}:
        return Verdict.blocked, "no API"

    if access in FRICTION_ACCESS:
        return Verdict.ready_with_friction, f"access is {access.value}"
    if access in OPEN_ACCESS:
        if api & HTTP_APIS and auth and not auth <= {AuthMethod.none}:
            return Verdict.ready, "self-serve credentials + REST/GraphQL API + known auth"
        if ApiType.sdk_or_cli_only in api:
            return Verdict.ready_with_friction, "self-serve, but SDK/CLI only (no HTTP API)"
        if api & HTTP_APIS and auth <= {AuthMethod.none} and auth:
            return Verdict.ready_with_friction, "self-serve HTTP API with no auth"
        return None, "self-serve, but API type or auth unknown"
    return None, "access unknown"
