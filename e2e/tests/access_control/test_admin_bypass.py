"""The access-control middleware's SUPER-ADMIN carve-out on an UNMAPPED route, e2e.

``ResourceGuardMiddleware`` fails a route with no configured resource closed for every
ordinary identity (``Forbidden: Route not configured``, 403) — but ADMITS the super-admin
discriminator (a condition-free ``["*"]`` policy that is not an owned key,
``is_admin=True``). Every OTHER auth-enabled e2e stack seeds a CATCH-ALL route table, so
this CASE-A path never fires there and a regression would be invisible; the
``admin_bypass_authz_stack`` boots a stack whose route table DELIBERATELY leaves
``/api/tools`` unmapped so the carve-out is exercised on a real route.
"""

from __future__ import annotations

import httpx

from tai42_e2e.stack import TaiStack

# The route the admin-bypass seed deliberately leaves with NO route row: a real, mounted,
# authenticated GET route (routers.tools, a pure in-process read) that is NOT an
# authenticated-always-allowed path — so it lands on the middleware's CASE A.
_UNMAPPED_ROUTE = "/api/tools"
# The one route the seed explicitly maps to the scoped key's sole scope (a pure
# in-process read, so it needs no backend worker — this stack runs none).
_MAPPED_ROUTE = "/api/manifest"
# The exact CASE-A deny body the middleware emits ({"error": <this>}).
_ROUTE_NOT_CONFIGURED = "Forbidden: Route not configured"


def _error_text(response: httpx.Response) -> str:
    """The ``error`` field of the middleware's JSON deny body, or the raw text if the
    body is not the ``{"error": ...}`` envelope (e.g. a 200 success)."""
    try:
        body = response.json()
    except ValueError:
        return response.text
    return body.get("error", "") if isinstance(body, dict) else response.text


async def test_super_admin_admitted_on_unmapped_route_others_denied(
    admin_bypass_authz_stack: tuple[TaiStack, str, str],
) -> None:
    """The middleware's CASE-A super-admin carve-out, end-to-end on a real unmapped route.

    (a) the super-admin ``["*"]`` key reaches the UNMAPPED route: NOT 403 and NOT the
    ``Route not configured`` body; (b) a non-admin scoped key on the SAME route is denied
    403 with that exact body; (c) the scoped key still reaches the one route it IS scoped
    for — so the denial in (b) is provably the not-configured path (CASE A), not a generic
    scope denial."""
    stack, admin_token, scoped_token = admin_bypass_authz_stack
    admin = stack.api().with_token(admin_token)
    scoped = stack.api().with_token(scoped_token)

    # (a) the super-admin is ADMITTED through the CASE-A carve-out.
    admitted = await admin.request_raw("GET", _UNMAPPED_ROUTE)
    assert admitted.status_code not in (401, 403), (
        f"super-admin was DENIED the unmapped route {_UNMAPPED_ROUTE} ({admitted.status_code}); "
        f"the CASE-A admin carve-out did not admit it: {admitted.text}"
    )
    assert _ROUTE_NOT_CONFIGURED not in _error_text(admitted), (
        f"super-admin got the not-configured deny body on the unmapped route {_UNMAPPED_ROUTE}: {admitted.text}"
    )

    # (b) a non-admin scoped key on the SAME route is denied CASE A — the exact body.
    denied = await scoped.request_raw("GET", _UNMAPPED_ROUTE)
    assert denied.status_code == 403, (
        f"non-admin scoped key on the unmapped route {_UNMAPPED_ROUTE} must be 403, got {denied.status_code}: "
        f"{denied.text}"
    )
    assert _error_text(denied) == _ROUTE_NOT_CONFIGURED, (
        f"non-admin deny on {_UNMAPPED_ROUTE} was not the not-configured body: {denied.text}"
    )

    # (c) the scoped key CAN reach the one route its scope maps — so the deny in (b) is the
    # not-configured path, not a blanket scope denial for this key.
    reachable = await scoped.request_raw("GET", _MAPPED_ROUTE)
    assert reachable.status_code not in (401, 403), (
        f"scoped key was denied its OWN mapped route {_MAPPED_ROUTE} ({reachable.status_code}); "
        f"the not-configured deny in (b) cannot be isolated to CASE A: {reachable.text}"
    )
