"""Authed user-administration routes — under the reserved ``/api/auth`` prefix.

Admin-only reach comes from the seeded jq conditions, so these handlers do not
re-check admin — except ``PUT /api/auth/users/me/password``, the one self-service
route open to every user. Handlers reach the injected services through
``service.provider_settings()``. Success bodies are ``{"data": ...}``; failures
are ``{"error": "<message>"}``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.access_control import get_current_user_id
from tai42_contract.app import tai42_app

from tai42_accounts_postgres import service
from tai42_accounts_postgres.hashing import HashCapacityError, hash_password_async, verify_password
from tai42_accounts_postgres.service import ADMIN_ROLE, SESSION_TOKEN_PREFIX
from tai42_accounts_postgres.settings import accounts_settings
from tai42_accounts_postgres.stores import EmailTakenError

logger = logging.getLogger(__name__)


class UserRecord(BaseModel):
    """One row of the user list — never a hash, never a token."""

    user_id: str
    email: str
    role: str
    disabled: bool
    created_at: str
    pending_invite: bool


class UsersListResponse(BaseModel):
    users: list[UserRecord]


class InviteResponse(BaseModel):
    """A minted/regenerated invite — raw token and origin-relative link, shown once."""

    user_id: str
    invite_token: str
    login_path: str


class CreateUserBody(BaseModel):
    email: str
    role: str


class UpdateUserBody(BaseModel):
    role: str | None = None
    disabled: bool | None = None


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def _now() -> datetime:
    return datetime.now(UTC)


async def _parse[BodyT: BaseModel](
    request: Request, model_cls: type[BodyT]
) -> tuple[BodyT | None, JSONResponse | None]:
    try:
        body = await request.json()
    except ValueError:
        return None, _error("invalid JSON body", 400)
    try:
        return model_cls.model_validate(body), None
    except ValidationError:
        return None, _error("invalid request body", 422)


def _serialize_user(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": row["user_id"],
        "email": row["email"],
        "role": row["role"],
        "disabled": row["disabled"],
        "created_at": row["created_at"].isoformat(),
        "pending_invite": row["pending_invite"],
    }


def _presented_session_hash(request: Request) -> str | None:
    """Hash of the caller's own session token so a self password change can spare
    it. Reads the bearer / X-Api-Key credential; ``None`` when not a session token."""
    token: str | None = None
    auth = request.headers.get("Authorization")
    if auth:
        scheme, _, rest = auth.partition(" ")
        if not rest:
            token = scheme
        elif scheme.lower() == "bearer":
            token = rest.strip() or None
    if token is None:
        token = request.headers.get("X-Api-Key")
    if token and token.startswith(SESSION_TOKEN_PREFIX):
        return service.token_hash(token)
    return None


@tai42_app.http.custom_route(
    "/api/auth/users",
    methods=["GET"],
    summary="List user accounts",
    tags=["users"],
    response_model=UsersListResponse,
    action="read",
)
async def list_users(request: Request) -> Response:
    """Every account. Never a password hash, never a token."""
    rows = await service.users_store().list()
    return JSONResponse({"data": {"users": [_serialize_user(row) for row in rows]}})


@tai42_app.http.custom_route(
    "/api/auth/users",
    methods=["POST"],
    summary="Create a user and mint an invite",
    tags=["users"],
    request_model=CreateUserBody,
    response_model=InviteResponse,
    action="write",
)
async def create_user(request: Request) -> Response:
    """Create an account with a NULL password and a one-time invite.

    Order: insert the user, apply its role, then mint the invite. If ``apply_role``
    fails after the row was written, the compensating cleanup deletes the row (and
    any invite) so creation stays re-runnable — never a policy-less zombie user.
    """
    body, error = await _parse(request, CreateUserBody)
    if error is not None:
        return error
    assert body is not None

    email = service.normalize_email(body.email)
    store = service.users_store()
    try:
        created_id = await store.create(service.new_user_id(), email, body.role, password_hash=None)
    except EmailTakenError:
        return _error("email already registered", 409)

    async def _cleanup() -> None:
        await service.invites_store().delete_for_user(created_id)
        await service.users_store().delete(created_id)

    try:
        await service.apply_role_compensated(created_id, body.role, cleanup=_cleanup)
    except KeyError:
        # Unknown role name; cleanup already removed the half-created row → clean 400.
        return _error(f"unknown role: {body.role!r}", 400)

    raw_invite = service.new_invite_token()
    expires_at = _now() + timedelta(seconds=accounts_settings().invite_ttl_seconds)
    await service.invites_store().create(service.token_hash(raw_invite), created_id, expires_at)
    return JSONResponse(
        {
            "data": {
                "user_id": created_id,
                "invite_token": raw_invite,
                "login_path": service.invite_login_path(raw_invite),
            }
        }
    )


@tai42_app.http.custom_route(
    "/api/auth/users/me/password",
    methods=["PUT"],
    summary="Change your own password",
    tags=["users"],
    request_model=ChangePasswordBody,
    response_model=None,
    action="write",
)
async def change_own_password(request: Request) -> Response:
    """Self password change: verify the current password, set the new one, and
    revoke every other session (the presented one survives)."""
    caller = get_current_user_id()
    if caller is None:
        return _error("unauthenticated", 401)

    body, error = await _parse(request, ChangePasswordBody)
    if error is not None:
        return error
    assert body is not None

    if len(body.new_password) < service.PASSWORD_MIN_LENGTH:
        return _error(f"Password must be at least {service.PASSWORD_MIN_LENGTH} characters", 422)

    user = await service.users_store().get_by_user_id(caller)
    if user is None or user["password_hash"] is None:
        return _error("no password set for this account", 400)

    try:
        matched = await verify_password(user["password_hash"], body.current_password)
    except HashCapacityError:
        return _error("Server busy, please retry shortly", 503)
    if not matched:
        return _error("current password is incorrect", 403)

    new_hash = await hash_password_async(body.new_password)
    await service.users_store().set_password_hash(caller, new_hash)
    await service.sessions_store().delete_for_user(caller, keep_token_hash=_presented_session_hash(request))
    return JSONResponse({"data": {"changed": True}})


@tai42_app.http.custom_route(
    "/api/auth/users/{user_id}",
    methods=["PUT"],
    summary="Update a user's role or disabled state",
    tags=["users"],
    request_model=UpdateUserBody,
    response_model=None,
    action="write",
)
async def update_user(request: Request) -> Response:
    """Change a user's role and/or disabled state.

    The last enabled admin cannot be demoted or disabled (409). A role change or a
    disable runs inside one advisory-locked transaction (``admin_guard_txn``) where
    the target's role/disabled are re-read from committed state under the lock, so
    concurrent last-admin removals cannot both pass.

    Disable uses credentials-die-first ordering (fail closed): the disable marker
    lands, then session revocation and the row write on the guard's own connection,
    so a mid-flow failure leaves credentials already dead. Re-enable reverses the
    order (marker last) and needs no guard.
    """
    user_id = request.path_params["user_id"]
    body, error = await _parse(request, UpdateUserBody)
    if error is not None:
        return error
    assert body is not None

    store = service.users_store()
    target = await store.get_by_user_id(user_id)
    if target is None:
        return _error("user not found", 404)
    admin = service.provider_settings().admin

    role_requested = body.role is not None and body.role != target["role"]
    disable_requested = body.disabled is not None and body.disabled != target["disabled"]

    # A role change or a disable could orphan the admins, so both run under the
    # lock. A re-enable combined with a role change enters here too (on
    # ``role_requested``) and is applied in both directions.
    if role_requested or (disable_requested and body.disabled):
        reenable_after_commit = False
        async with store.admin_guard_txn() as guard:
            locked = await guard.read_target(user_id)
            if locked is None:
                return _error("user not found", 404)
            current_role = locked["role"]

            if body.role is not None and body.role != current_role:
                demote = current_role == ADMIN_ROLE and body.role != ADMIN_ROLE
                if demote and not locked["disabled"] and await guard.count_other_enabled_admins(user_id) == 0:
                    return _error("cannot demote the last enabled admin", 409)
                try:
                    await admin.apply_role(user_id, body.role)
                except KeyError:
                    # Unknown role name; ``apply_role`` wrote nothing → clean 400.
                    return _error(f"unknown role: {body.role!r}", 400)
                await guard.set_role(user_id, body.role)
                current_role = body.role

            if body.disabled and not locked["disabled"]:
                # DISABLE. Credentials die first (marker + sessions), then the row.
                if current_role == ADMIN_ROLE and await guard.count_other_enabled_admins(user_id) == 0:
                    return _error("cannot disable the last enabled admin", 409)
                await admin.set_user_disabled(user_id, True)
                await guard.delete_sessions_for_user(user_id)
                await guard.set_disabled(user_id, True)
            elif disable_requested and body.disabled is False and locked["disabled"]:
                # RE-ENABLE (combined with a role change): row first, marker after
                # commit (mirror of disable); adding an admin needs no orphan check.
                await guard.set_disabled(user_id, False)
                reenable_after_commit = True

        if reenable_after_commit:
            await admin.set_user_disabled(user_id, False)
    elif disable_requested:
        # Pure re-enable: row first, marker last (the mirror of disable).
        await store.set_disabled(user_id, False)
        await admin.set_user_disabled(user_id, False)

    return JSONResponse({"data": {"user_id": user_id}})


@tai42_app.http.custom_route(
    "/api/auth/users/{user_id}",
    methods=["DELETE"],
    summary="Delete a user",
    tags=["users"],
    response_model=None,
    action="write",
)
async def delete_user(request: Request) -> Response:
    """Delete a user. The last enabled admin cannot be deleted (409); the guard
    re-reads role/disabled under the advisory lock and deletes inside one
    transaction, so concurrent last-admin removals cannot both pass. Order: remove
    the policy (revoking owned keys), then sessions, invites, and the user row — all
    on the guard's connection. A mid-way failure leaves a re-deletable user, never
    an orphaned live credential."""
    user_id = request.path_params["user_id"]
    store = service.users_store()
    target = await store.get_by_user_id(user_id)
    if target is None:
        return _error("user not found", 404)
    admin = service.provider_settings().admin

    async with store.admin_guard_txn() as guard:
        locked = await guard.read_target(user_id)
        if (
            locked is not None
            and locked["role"] == ADMIN_ROLE
            and not locked["disabled"]
            and await guard.count_other_enabled_admins(user_id) == 0
        ):
            return _error("cannot delete the last enabled admin", 409)
        await admin.remove_policy(user_id)
        await guard.delete_sessions_for_user(user_id)
        await guard.delete_invites_for_user(user_id)
        await guard.delete(user_id)

    return JSONResponse({"data": {"deleted": True, "user_id": user_id}})


@tai42_app.http.custom_route(
    "/api/auth/users/{user_id}/invite",
    methods=["POST"],
    summary="Regenerate a user's invite",
    tags=["users"],
    response_model=InviteResponse,
    action="write",
)
async def regenerate_invite(request: Request) -> Response:
    """Replace the live invite for a user who has not set a password yet (409 once
    a password is set — a set password rotates through the self-service route)."""
    user_id = request.path_params["user_id"]
    target = await service.users_store().get_by_user_id(user_id)
    if target is None:
        return _error("user not found", 404)
    if target["password_hash"] is not None:
        return _error("user already has a password", 409)

    raw_invite = service.new_invite_token()
    expires_at = _now() + timedelta(seconds=accounts_settings().invite_ttl_seconds)
    await service.invites_store().create(service.token_hash(raw_invite), user_id, expires_at)
    return JSONResponse(
        {
            "data": {
                "user_id": user_id,
                "invite_token": raw_invite,
                "login_path": service.invite_login_path(raw_invite),
            }
        }
    )
