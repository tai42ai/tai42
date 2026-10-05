"""The access-control middleware's CASE-A carve-out and the universal-scope reach, e2e.

Two guards, exercised on a stack whose route table seeds NO catch-all (every OTHER
auth-enabled stack does, hiding these paths):

* CASE A — a path the application does not SERVE (no registered route, no route row) fails
  closed for every ordinary identity (``Forbidden: Route not configured``, 403) but ADMITS
  the super-admin discriminator (a condition-free ``["*"]`` policy that is not an owned key,
  ``is_admin=True``), which then meets the router's own 404.
* A REGISTERED, authenticated route the operator mapped to NO row resolves to the universal
  scope ``"*"``: a role-holder (the admin ``["*"]`` key here) reaches it out of the box,
  while a non-``*`` scoped key is denied for INSUFFICIENT SCOPE — a 403 that is NOT the
  not-configured body, since the route IS served.
"""

from __future__ import annotations

import httpx
import pytest

from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs("kind:identity", "store:postgres", "store:redis", "setting:seeded-access-control")

# A path the application does not serve at all (no registered route, no row) — the only
# shape that still lands on the middleware's CASE A after the declared-protection tier.
_UNREGISTERED_ROUTE = "/api/no-such-route"
# A REAL, authenticated GET route (routers.tools, a pure in-process read) the seed leaves
# with NO route row: it resolves to the universal scope, reached by the admin ``*`` key and
# denied (scope-miss, NOT not-configured) to the scoped key.
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


async def test_case_a_carve_out_and_universal_scope_on_a_registered_unmapped_route(
    admin_bypass_authz_stack: tuple[TaiStack, str, str],
) -> None:
    """CASE A on an UNREGISTERED path, and the universal-scope reach on a REGISTERED unmapped route.

    (a) CASE A — on a path the app does not serve: the super-admin ``["*"]`` key is admitted
    through the carve-out (NOT 401/403; it meets the router's own 404), and a non-admin scoped
    key is denied 403 with the exact ``Route not configured`` body. (b) on a REGISTERED,
    authenticated route the operator mapped to no row: the admin ``["*"]`` key reaches it
    (NOT 401/403) because it resolves to the universal scope, while the scoped key is denied
    403 for insufficient scope — NOT the not-configured body, since the route IS served.
    (c) the scoped key still reaches the one route its scope maps — so the deny in (b) is a
    scope-miss on a served route, not a CASE-A not-configured deny."""
    stack, admin_token, scoped_token = admin_bypass_authz_stack
    admin = stack.api().with_token(admin_token)
    scoped = stack.api().with_token(scoped_token)

    # (a) CASE A on an UNREGISTERED path: admin admitted (then the router 404s), scoped denied
    # 403 with the exact not-configured body.
    admin_unregistered = await admin.request_raw("GET", _UNREGISTERED_ROUTE)
    assert admin_unregistered.status_code not in (401, 403), (
        f"super-admin was DENIED the unregistered path {_UNREGISTERED_ROUTE} ({admin_unregistered.status_code}); "
        f"the CASE-A admin carve-out did not admit it: {admin_unregistered.text}"
    )
    scoped_unregistered = await scoped.request_raw("GET", _UNREGISTERED_ROUTE)
    assert scoped_unregistered.status_code == 403, (
        f"non-admin scoped key on the unregistered path {_UNREGISTERED_ROUTE} must be 403, "
        f"got {scoped_unregistered.status_code}: {scoped_unregistered.text}"
    )
    assert _error_text(scoped_unregistered) == _ROUTE_NOT_CONFIGURED, (
        f"non-admin deny on {_UNREGISTERED_ROUTE} was not the not-configured body: {scoped_unregistered.text}"
    )

    # (b) a REGISTERED authed route with no row resolves to the universal scope: the admin
    # key reaches it, the scoped key is denied 403 but NOT with the not-configured body.
    admitted = await admin.request_raw("GET", _UNMAPPED_ROUTE)
    assert admitted.status_code not in (401, 403), (
        f"admin ['*'] key was DENIED the registered unmapped route {_UNMAPPED_ROUTE} ({admitted.status_code}); "
        f"the declared-protection tier did not resolve it to the universal scope: {admitted.text}"
    )
    denied = await scoped.request_raw("GET", _UNMAPPED_ROUTE)
    assert denied.status_code == 403, (
        f"non-admin scoped key on the registered unmapped route {_UNMAPPED_ROUTE} must be 403, "
        f"got {denied.status_code}: {denied.text}"
    )
    assert _error_text(denied) != _ROUTE_NOT_CONFIGURED, (
        f"scoped-key deny on the REGISTERED route {_UNMAPPED_ROUTE} was the not-configured body — it should be a "
        f"scope-miss, since the route is served: {denied.text}"
    )

    # (c) the scoped key CAN reach the one route its scope maps — so the deny in (b) is a
    # scope-miss on a served route, not a CASE-A not-configured deny.
    reachable = await scoped.request_raw("GET", _MAPPED_ROUTE)
    assert reachable.status_code not in (401, 403), (
        f"scoped key was denied its OWN mapped route {_MAPPED_ROUTE} ({reachable.status_code}): {reachable.text}"
    )
