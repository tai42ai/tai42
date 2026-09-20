"""The admin-only principal-management operations.

A principal is the unit of identity and authority every credential belongs to. These
doors create/list/update/delete the ``human`` and ``service`` principals an operator
manages: ``GET/POST /api/auth/principals`` and ``PUT/DELETE /api/auth/principals/{user_id}``.
They live under ``/api/auth`` (the control-plane gate admin-gates them); the op-level
``require_admin`` is defense in depth. With access control off they, like every other rule,
allow: there is no principal to classify and the surface is already reachable.

Who may disable or delete a principal is decided by OWNERSHIP OF ITS LOGIN, not its kind.
A ``disabled`` change or a DELETE first asks every registered login-attaching accounts
provider whether it holds a login for the principal; if one does (a password human), the
change is refused with a 409 and routed to that provider's users door, which cleans the
login row and runs its own last-admin guard. Every principal no attaching provider claims
— a ``service`` principal, an OIDC-provisioned human whose login lives at the issuer, the
keys-only owner — is disabled or deleted here, behind a skeleton last-admin guard that
refuses to strand the deployment with no enabled admin. A display-name edit is accepted
for any kind.
"""

from __future__ import annotations

from secrets import token_urlsafe
from typing import Any, Literal

from pydantic import BaseModel, Field, RootModel
from tai42_contract.access_control.models import Principal
from tai42_contract.accounts import LoginAttachingProvider

from tai42_skeleton.access_control import management, roles
from tai42_skeleton.access_control.store import access_control_store
from tai42_skeleton.operations import BadRequestError, ConflictError, ForbiddenError, NotFoundError, operation
from tai42_skeleton.operations._authority import require_admin, resolve_caller


class PrincipalCreate(BaseModel):
    """The create-principal request body.

    ``user_id`` is optional — the door mints one when absent. ``kind`` is ``human`` or
    ``service``; ``role`` names the role template applied to the principal's policy.
    """

    user_id: str | None = None
    kind: Literal["human", "service"]
    display_name: str = Field(min_length=1)
    role: str = Field(min_length=1)


class PrincipalUpdate(BaseModel):
    """The update-principal request body: display name and/or disabled, both omit-means-keep."""

    display_name: str | None = Field(default=None, min_length=1)
    disabled: bool | None = None


class PrincipalListing(RootModel[list[Principal]]):
    """Every principal the operator manages."""


class PrincipalDeleted(BaseModel):
    """Acknowledges a principal deletion."""

    user_id: str
    deleted: bool


def _login_attaching_providers() -> list[tuple[str, LoginAttachingProvider]]:
    """The CURRENT epoch's live login-attaching accounts providers, name-sorted.

    The same registry :mod:`~tai42_skeleton.operations.setup` and
    :mod:`~tai42_skeleton.operations.login` read: the serving core's recorded auth
    providers, filtered to the ones that can attach — and therefore hold — an
    interactive login. Name-sorted so the owning-provider choice is deterministic.
    """
    from tai42_skeleton.app.instance import app

    recorded = app._serving_core.active_auth_providers
    return [(name, p) for name, p in sorted(recorded.items()) if isinstance(p, LoginAttachingProvider)]


async def _refuse_if_login_owned(user_id: str) -> None:
    """Refuse (409) when a login-attaching provider holds this principal's login.

    A principal whose interactive login lives in an accounts provider is disabled and
    deleted through THAT provider's users door — which cleans the login row and runs its
    own last-admin guard — never this door. A provider store error propagates (fail
    closed), never a silent pass.
    """
    for name, provider in _login_attaching_providers():
        if await provider.has_login(user_id):
            raise ConflictError(
                f"principal {user_id!r} is managed through the {name!r} accounts provider that owns its "
                "login; disable or delete it through that provider's users door, not this one"
            )


async def _refuse_if_last_admin(guard: Any, user_id: str, target_disabled: bool) -> None:
    """Refuse (409), rolling the guard's transaction back, when removing ``user_id`` strands the admins.

    Runs INSIDE the advisory-locked guard transaction so the last-admin count and the
    mutation it gates are atomic — a concurrent guarded removal blocks at the lock and
    re-counts committed state, so two removals of the last two enabled admins cannot both
    pass. Guards ONLY a currently-enabled admin target: a target already disabled
    (``target_disabled``) or non-admin never contributes to the enabled-admin count, so
    removing it strands nothing. When the target is an enabled admin, the guard counts the
    OTHER enabled admin principals on its locked cursor; a count of zero makes the target the
    last one and the door refuses (the ``ConflictError`` propagates out of the guard block,
    rolling the transaction back with nothing written).
    """
    if target_disabled:
        return
    if await guard.target_is_enabled_admin(user_id) and await guard.count_other_enabled_admins(user_id) == 0:
        raise ConflictError("the last enabled admin principal cannot be disabled or deleted")


@operation(summary="List principals", tags=["access-control"], errors=[ForbiddenError], response_model=PrincipalListing)
async def list_principals() -> PrincipalListing:
    """Every principal (admin only)."""
    caller = await resolve_caller()
    require_admin(caller)
    return PrincipalListing.model_validate(await management.list_principals())


@operation(
    summary="Create a principal",
    tags=["access-control"],
    authority_changing=True,
    errors=[BadRequestError, ForbiddenError, ConflictError],
    request_model=PrincipalCreate,
    response_model=Principal,
)
async def create_principal(user_id: str | None, kind: str, display_name: str, role: str) -> Principal:
    """Create a ``human`` or ``service`` principal and apply its role (admin only).

    The door mints a ``user_id`` when absent. A duplicate id is a 409; an unknown role is a
    400. ``created_by`` records the admin who created it.
    """
    caller = await resolve_caller()
    require_admin(caller)
    resolved_id = user_id or f"usr-{token_urlsafe(8)}"
    try:
        created = await roles.create_principal(
            resolved_id, kind=kind, display_name=display_name, created_by=caller.caller_id, role=role
        )
    except ValueError as exc:
        raise ConflictError(str(exc)) from exc
    except KeyError as exc:
        raise BadRequestError(f"unknown role: {role!r}") from exc
    return Principal.model_validate(created)


@operation(
    summary="Update a principal",
    tags=["access-control"],
    authority_changing=True,
    errors=[BadRequestError, ConflictError, ForbiddenError, NotFoundError],
    request_model=PrincipalUpdate,
    response_model=Principal,
)
async def update_principal(user_id: str, display_name: str | None = None, disabled: bool | None = None) -> Principal:
    """Update a principal's display name and/or disabled state (admin only).

    Both fields are omit-means-keep. A ``disabled`` change is refused (409) when a
    login-attaching accounts provider owns the principal's login — its disabled state lives
    at that provider's users door — and otherwise flips both homes through the single
    disabled writer. Disabling the last enabled admin principal is refused (409) behind the
    store's advisory-locked guard. A display-name edit is accepted for any kind. Returns the
    updated principal; a missing principal is a 404.
    """
    caller = await resolve_caller()
    require_admin(caller)
    if display_name is None and disabled is None:
        raise BadRequestError("nothing to change: provide 'display_name' and/or 'disabled'")
    store = access_control_store()
    principal = await store.get_principal(user_id)
    if principal is None:
        raise NotFoundError(f"principal not found: {user_id!r}")
    if disabled is not None:
        await _refuse_if_login_owned(user_id)
        async with store.principal_guard_txn() as guard:
            locked = await guard.read(user_id)
            if locked is None:
                raise NotFoundError(f"principal not found: {user_id!r}")
            if disabled:
                await _refuse_if_last_admin(guard, user_id, locked["disabled"])
            committed = await guard.set_disabled(user_id, disabled)
        await roles.refresh_enforcement_after_disabled(user_id, committed)
    if display_name is not None:
        await store.update_principal_display_name(user_id, display_name)
    updated = await store.get_principal(user_id)
    if updated is None:
        raise NotFoundError(f"principal not found: {user_id!r}")
    return Principal.model_validate(updated)


@operation(
    summary="Delete a principal",
    tags=["access-control"],
    authority_changing=True,
    destructive=True,
    errors=[ConflictError, ForbiddenError, NotFoundError],
    response_model=PrincipalDeleted,
)
async def delete_principal(user_id: str) -> dict[str, Any]:
    """Delete a principal, revoking every key it owns plus its policy row (admin only).

    Refused (409) when a login-attaching accounts provider owns the principal's login — that
    provider's users door deletes it and cleans the login row. A principal no attaching
    provider claims (service, OIDC-provisioned human, keys-only owner) is deleted here;
    deleting the last enabled admin principal is refused (409) behind the store's
    advisory-locked guard. A missing principal is a 404.
    """
    caller = await resolve_caller()
    require_admin(caller)
    store = access_control_store()
    principal = await store.get_principal(user_id)
    if principal is None:
        raise NotFoundError(f"principal not found: {user_id!r}")
    await _refuse_if_login_owned(user_id)
    async with store.principal_guard_txn() as guard:
        locked = await guard.read(user_id)
        if locked is None:
            raise NotFoundError(f"principal not found: {user_id!r}")
        await _refuse_if_last_admin(guard, user_id, locked["disabled"])
        policy_existed, principal_existed = await guard.delete(user_id)
        if not policy_existed or not principal_existed:
            raise NotFoundError(f"principal not found: {user_id!r}")
    await roles.revoke_owned_keys_and_bump(user_id)
    return {"user_id": user_id, "deleted": True}
