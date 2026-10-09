"""The self-service password route — under the reserved ``/api/auth`` prefix.

``PUT /api/auth/users/me/password`` is the one self-service route open to every user: a
person changing their OWN password. It is NOT a member-admin action (it acts on the
caller's own credential, targets no member row, and is not admin-only), so it is not part
of the generic member-actions seam; the admin member-action capability is declared through
:meth:`~tai42_accounts_postgres.provider.PostgresAccountsProvider.member_actions`. Admin
reach for the surrounding ``/api/auth`` surface comes from the seeded jq conditions; this
handler carves itself out with ``self_service=True``. The handler reaches the injected
services through ``service``. Success bodies are ``{"data": ...}``; failures are
``{"error": "<message>"}``.
"""

from __future__ import annotations

from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.access_control import get_current_user_id
from tai42_contract.app import tai42_app

# Arms the accounts backup-section on_startup hook. Homed HERE (a manifest-loaded
# router module the host imports only post-bind) and NOT in the package __init__ —
# a pre-bind package import must never touch the bound handle. Idempotent: both
# router modules arm it; the hook's registry guard makes the second a no-op.
from tai42_accounts_postgres import backup as _backup  # noqa: F401
from tai42_accounts_postgres import service
from tai42_accounts_postgres.hashing import HashCapacityError, hash_password_async, verify_password
from tai42_accounts_postgres.service import SESSION_TOKEN_PREFIX


class PasswordChangedResponse(BaseModel):
    """Ack of a self-service password change — always ``changed: true`` on success."""

    changed: bool


class ChangePasswordBody(BaseModel):
    """The change-password request body: the ``current_password`` and the ``new_password``."""

    current_password: str
    new_password: str


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


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


def _presented_session_hash(request: Request) -> str | None:
    """Hash of the session the caller authenticated with, so a self password change can spare it.

    Reads the credential the access-control gate verified; ``None`` when it is not a session.
    """
    credential = tai42_app.accounts.authenticated_credential(request)
    if credential is not None and credential.startswith(SESSION_TOKEN_PREFIX):
        return service.token_hash(credential)
    return None


@tai42_app.http.custom_route(
    "/users/me/password",
    methods=["PUT"],
    summary="Change your own password",
    tags=["users"],
    request_model=ChangePasswordBody,
    response_model=PasswordChangedResponse,
    action="write",
    # The one self-service route under the admin-gated users surface: the platform carves
    # it into the default editor/viewer reach from this flag, so a non-admin may change
    # their own password while the rest of the surface stays admin-only.
    self_service=True,
)
async def change_own_password(request: Request) -> Response:
    """Self password change: verify the current password, set the new one, and revoke every other session.

    The session the caller authenticated with survives.
    """
    caller = get_current_user_id()
    if caller is None:
        return _error("unauthenticated", 401)

    body, error = await _parse(request, ChangePasswordBody)
    if error is not None:
        return error
    if body is None:
        raise AssertionError

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
