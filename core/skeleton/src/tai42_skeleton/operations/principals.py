"""The admin-only principal-management operations.

A principal is the unit of identity and authority every credential belongs to. These
doors create/list/update/delete the ``human`` and ``service`` principals an operator
manages: ``GET/POST /api/auth/principals`` and ``PUT/DELETE /api/auth/principals/{user_id}``.
They live under ``/api/auth`` (the control-plane gate admin-gates them); the op-level
``require_admin`` is defense in depth. With access control off they, like every other rule,
allow: there is no principal to classify and the surface is already reachable.

Who may DELETE a principal is decided by ownership of its login record; who may DISABLE it by
ownership of its interactive credential — never by its kind. A DELETE first asks every
registered login-owning accounts provider whether it keeps a login record for the principal;
a ``disabled`` change asks every login-attaching provider (one that holds the credential and
its disabled state). If one claims the principal, the change is refused with a 409 naming that
provider's member actions on the Members page, which remove the principal and the record
together (or disable it with the credential). A human whose login record a provider keeps but
whose credential lives elsewhere (an OIDC login attached by its provider) is disabled here and
deleted through that provider's member actions; a principal no provider claims — a ``service``
principal, a hand-provisioned OIDC human, the keys-only owner — is disabled and deleted here.
Both doors hold the one last-admin guard, which refuses to strand the deployment with no
enabled admin principal. A display-name edit is accepted for any kind.
"""

from __future__ import annotations

from collections.abc import Sequence
from secrets import token_urlsafe
from typing import Any, Literal

from pydantic import BaseModel, Field, RootModel
from tai42_contract.access_control.models import Principal
from tai42_contract.accounts import LoginAttachingProvider, LoginOwningProvider
from tai42_contract.accounts.errors import LastAdminError

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


def _recorded_providers() -> list[tuple[str, object]]:
    """The CURRENT epoch's live auth providers, name-sorted for a deterministic owner choice.

    The same registry :mod:`~tai42_skeleton.operations.setup` and
    :mod:`~tai42_skeleton.operations.login` read: the app's recorded auth providers.
    """
    from tai42_skeleton.app.instance import app

    return sorted(app.recorded_auth_providers().items())


def _login_owning_providers() -> list[tuple[str, LoginOwningProvider]]:
    """The live accounts providers that keep login records — the ones asked before a DELETE."""
    return [(name, p) for name, p in _recorded_providers() if isinstance(p, LoginOwningProvider)]


def _login_attaching_providers() -> list[tuple[str, LoginAttachingProvider]]:
    """The live accounts providers that hold an interactive credential — the ones asked before a DISABLE."""
    return [(name, p) for name, p in _recorded_providers() if isinstance(p, LoginAttachingProvider)]


async def _refuse_if_claimed(
    user_id: str, providers: Sequence[tuple[str, LoginOwningProvider]], *, change: str
) -> None:
    """Refuse (409) when one of ``providers`` claims this principal's login.

    ``change`` is the instruction for the owner's door (``"remove it"``, ``"disable or enable
    it"``). A provider store error propagates (fail closed), never a silent pass.
    """
    for name, provider in providers:
        if await provider.has_login(user_id):
            raise ConflictError(
                f"principal {user_id!r} is managed through the {name!r} accounts provider that owns its login; "
                f"{change} through that provider's member actions on the Members page, not through this door"
            )


# The door's refusal text when a disable or delete would strand the admins.
_LAST_ADMIN_CONFLICT = "the last enabled admin principal cannot be disabled or deleted"


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
    login-attaching accounts provider holds the principal's credential — its disabled state
    lives with that provider and changes through its member actions on the Members page — and
    otherwise flips both homes through the guarded disabled writer (a principal whose login
    record a provider keeps without holding its credential is disabled here). Disabling the
    last enabled admin principal is refused (409) by the last-admin guard. A display-name edit
    is accepted for any kind. Returns the updated principal; a missing principal is a 404.
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
        await _refuse_if_claimed(user_id, _login_attaching_providers(), change="disable or enable it")
        try:
            await roles.set_principal_disabled(user_id, disabled)
        except LastAdminError as exc:
            raise ConflictError(_LAST_ADMIN_CONFLICT) from exc
        except KeyError as exc:
            raise NotFoundError(f"principal not found: {user_id!r}") from exc
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

    Refused (409) when a login-owning accounts provider keeps a login record for the principal
    — that provider's member actions on the Members page remove the principal and the record
    together, so the record never outlives the principal. A principal no provider claims
    (service, hand-provisioned OIDC human, keys-only owner) is deleted here; deleting the last
    enabled admin principal is refused (409) by the last-admin guard. A missing principal is a
    404.
    """
    caller = await resolve_caller()
    require_admin(caller)
    store = access_control_store()
    principal = await store.get_principal(user_id)
    if principal is None:
        raise NotFoundError(f"principal not found: {user_id!r}")
    await _refuse_if_claimed(user_id, _login_owning_providers(), change="remove it")
    try:
        await roles.delete_principal(user_id)
    except LastAdminError as exc:
        raise ConflictError(_LAST_ADMIN_CONFLICT) from exc
    except KeyError as exc:
        raise NotFoundError(f"principal not found: {user_id!r}") from exc
    return {"user_id": user_id, "deleted": True}
