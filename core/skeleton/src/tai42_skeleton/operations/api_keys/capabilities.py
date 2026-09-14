"""Mint-capability + role-template read doors: report mint capabilities, list roles."""

from __future__ import annotations

from typing import Any

import tai42_skeleton.operations.api_keys as _pkg
from tai42_skeleton.access_control import management
from tai42_skeleton.access_control.roles import role_store
from tai42_skeleton.operations import ForbiddenError, operation
from tai42_skeleton.operations._authority import require_admin
from tai42_skeleton.operations.response_models_group_a import MintCapabilities, RoleDefinitionList


@operation(summary="Report key-mint capabilities", tags=["access-control"], response_model=MintCapabilities)
async def get_capabilities() -> dict[str, Any]:
    """Whether any configured identity provider can MINT keys, per provider. Lets the
    Studio disable mint UI with a clear message on a validator-only deployment instead
    of surfacing a raw error at mint time."""
    capabilities = management.provider_capabilities()
    providers = [{"name": name, "mintable": mintable} for name, mintable in capabilities]
    return {"mintable": any(m for _, m in capabilities), "providers": providers}


@operation(summary="List roles", tags=["access-control"], errors=[ForbiddenError], response_model=RoleDefinitionList)
async def list_roles() -> list[dict[str, Any]]:
    """The seeded/operator-authored roles as full ``RoleDefinition``-shaped bodies
    (``{name, description, scopes, condition, base_tier, allow_all, grants}``) — the
    users-admin role picker and the Studio Roles page read
    this. A store-less deployment (no versioned store configured) has no roles — the seed
    step is skipped at boot — so the read is skipped and the list is empty.

    Admin-only: a listing exposes every role's raw base-tier jq condition, so a non-admin
    caller is denied 403. This op-level check is defense in depth behind the ``secret``
    route action-class that already fences the route."""
    # OFF: access control disabled → no roles are seeded; the honest empty list, never a
    # store read under the synthetic admin.
    if not _pkg.access_control_settings().enable:
        return []
    caller = await _pkg.resolve_caller()
    require_admin(caller)
    from tai42_kit.db import component_store_configured

    from tai42_skeleton.db import SKELETON_COMPONENT

    if not component_store_configured(SKELETON_COMPONENT):
        return []
    return await role_store().list_roles()
