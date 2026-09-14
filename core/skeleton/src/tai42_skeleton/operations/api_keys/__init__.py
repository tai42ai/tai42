"""Operations for the access-control keys/scopes surface — the authed doors the
Studio's API-keys settings tab consumes, projected from one declaration.

Nineteen operations for the access-control keys/scopes surface: the scope
catalog + CRUD, the route catalog, the public-route pins, the key CRUD (create /
edit / revoke), one-time claim-link creation, the mint-capability + role reads, the
caller's own capability projection (``get_me``), the fail-closed condition validator,
and the admin-only policy version-history + rollback. Most route their core work
through the access-control ``management`` module; ``create_claim_link`` delegates to
:mod:`~tai42_skeleton.access_control.claim_links` and ``get_me`` to
:mod:`~tai42_skeleton.access_control.projection`. Every one is a route under
``/api/auth/*``, so the eighteen non-``get_me`` ops are tier-2 (default-excluded from the MCP surface,
includable by an explicit ``api_tools.include``) by the projection module's
route-prefix predicate — no ``authority_changing`` flag needed. ``get_me`` is
tier-1 (``caller_context=True``, NEVER projectable): its params are the caller's own
edge-derived identity, which an MCP caller could supply to spoof another principal.

**Owner-aware ownership rules.** The caller is resolved from the request-scoped
``user_id`` contextvar and classified admin/non-admin: admin iff a condition-free
``"*"`` policy that is not itself an owned key. A non-admin may create only self-owned
keys with scopes ⊆ its own; may edit/revoke only keys it owns; and sees only its own
keys in ``tokens-payload``. An owned key can mint nothing. A key's owner claim is
immutable through the edit surface (re-mint to change ownership). With the gate off the
caller is treated as admin (nothing to attenuate against).

Every mutation bumps the policy version so a running worker's policy cache re-reads
the edit instead of serving a stale copy. Each policy write is ordered
enforced-store first (the ``management`` write lands, then the cache-buster bump,
then the durable PG version history) so enforcement is current the instant the
authority changes even if the audit write then fails.

The test-double seam symbols ``resolve_caller``, ``ac_policy_store``,
``build_projection``, ``access_control_settings`` and ``_record_policy_version`` are
bound as package attributes so a ``setattr(operations.api_keys, "<sym>", …)`` takes
effect: every door reads them THROUGH this package object at call time.
"""

from __future__ import annotations

import sys

from tai42_skeleton.access_control.policy_store import ac_policy_store
from tai42_skeleton.access_control.projection import build_projection
from tai42_skeleton.access_control.settings import access_control_settings
from tai42_skeleton.operations._authority import resolve_caller

# A reload can rebuild THIS package around a still-cached door submodule whose module-top
# ``_pkg`` alias then points at the retired generation. Re-point each seam-reading door
# submodule's alias at THIS package object so a door always reads the seam symbols from the
# generation that re-exports it — the generation a test patches at the package alias.
from . import capabilities, keys, me, ownership, policy_history, routes, scopes
from .capabilities import get_capabilities, list_roles
from .conditions import validate_condition
from .keys import (
    create_api_key,
    create_claim_link,
    edit_api_key,
    list_tokens_payload,
    modify_api_key_scopes,
    revoke_api_key,
)
from .me import get_me
from .ownership import _check_scope_subset, _record_policy_version
from .policy_history import list_policy_versions, rollback_policy
from .routes import list_public_routes, list_routes, pin_public_route, unpin_public_route
from .scopes import add_scope_url, delete_scope, list_scopes, remove_scope_url

for _door_submodule in (capabilities, keys, me, ownership, policy_history, routes, scopes):
    _door_submodule.__dict__["_pkg"] = sys.modules[__name__]

__all__ = [
    "_check_scope_subset",
    "_record_policy_version",
    "ac_policy_store",
    "access_control_settings",
    "add_scope_url",
    "build_projection",
    "create_api_key",
    "create_claim_link",
    "delete_scope",
    "edit_api_key",
    "get_capabilities",
    "get_me",
    "list_policy_versions",
    "list_public_routes",
    "list_roles",
    "list_routes",
    "list_scopes",
    "list_tokens_payload",
    "modify_api_key_scopes",
    "pin_public_route",
    "remove_scope_url",
    "resolve_caller",
    "revoke_api_key",
    "rollback_policy",
    "unpin_public_route",
    "validate_condition",
]
