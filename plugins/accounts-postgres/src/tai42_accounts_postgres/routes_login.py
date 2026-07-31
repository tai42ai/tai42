"""Public login routes — the ``/api/login/*`` namespace.

Handlers take no settings argument: they reach the injected services through
``service.provider_settings()``. Success bodies are ``{"data": {"token": raw,
"user_id": ...}}``. Login failure is uniform: unknown email and wrong password
both return a generic 401 and run a real argon2 verify so timing does not
enumerate users. Also registers the AC-required boot guard.
"""

from __future__ import annotations

import logging
import math
import secrets
from datetime import UTC, datetime

from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app

from tai42_accounts_postgres import service
from tai42_accounts_postgres.hashing import (
    DUMMY_HASH,
    HashCapacityError,
    hash_password_async,
    verify_password,
)
from tai42_accounts_postgres.rate_limit import RateLimitedError, RateLimiter
from tai42_accounts_postgres.settings import accounts_settings

logger = logging.getLogger(__name__)


class SessionResponse(BaseModel):
    """The one-time login result: a raw session token and the user's id."""

    token: str
    user_id: str


class PasswordLoginBody(BaseModel):
    """Credentials for ``POST /api/login/password``."""

    email: str
    password: str


class BootstrapBody(BaseModel):
    """First-owner creation for ``POST /api/login/bootstrap``."""

    email: str
    password: str
    bootstrap_token: str | None = None


class InviteAcceptBody(BaseModel):
    """Set-your-password for ``POST /api/login/invite/accept``."""

    invite_token: str
    password: str
    password_confirm: str


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def _now() -> datetime:
    return datetime.now(UTC)


def _client_ip(request: Request) -> str:
    # The direct peer — no X-Forwarded-For parsing.
    return request.client.host if request.client else "unknown"


def _limiter() -> RateLimiter:
    return RateLimiter(service.provider_settings().redis, accounts_settings())


def _session_response(raw_token: str, user_id: str) -> JSONResponse:
    return JSONResponse({"data": {"token": raw_token, "user_id": user_id}})


def _throttled_response(exc: RateLimitedError) -> JSONResponse:
    return JSONResponse(
        {"error": service.too_many_attempts_message(exc.retry_after)},
        status_code=429,
        headers={"Retry-After": str(exc.retry_after)},
    )


def _hash_capacity_response() -> JSONResponse:
    retry_after = max(1, math.ceil(accounts_settings().login_hash_wait_seconds))
    return JSONResponse(
        {"error": "Server busy, please retry shortly"},
        status_code=503,
        headers={"Retry-After": str(retry_after)},
    )


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


def _password_too_short(password: str) -> str | None:
    if len(password) < service.PASSWORD_MIN_LENGTH:
        return f"Password must be at least {service.PASSWORD_MIN_LENGTH} characters"
    return None


@tai42_app.http.custom_route(
    "/api/login/password",
    methods=["POST"],
    summary="Log in with email and password",
    tags=["login"],
    request_model=PasswordLoginBody,
    response_model=SessionResponse,
    authed=False,
)
async def login_password(request: Request) -> Response:
    """Verify email + password and mint a session.

    Throttling is failures-only. Unknown email, a NULL password hash (un-accepted
    invite), a disabled account, and a wrong password all return the same generic
    401 and all run a real argon2 verify so timing is uniform.
    """
    body, error = await _parse(request, PasswordLoginBody)
    if error is not None:
        return error
    assert body is not None

    email = service.normalize_email(body.email)
    ip = _client_ip(request)
    store = service.users_store()
    user = await store.get_by_email(email)

    try:
        if user is None or user["password_hash"] is None or user["disabled"]:
            # Uniform argon2 work on every non-viable account (timing safety).
            await verify_password(DUMMY_HASH, body.password)
            matched = False
        else:
            matched = await verify_password(user["password_hash"], body.password)
    except HashCapacityError:
        return _hash_capacity_response()

    if not matched:
        try:
            await _limiter().record_failure(email, ip)
        except RateLimitedError as exc:
            return _throttled_response(exc)
        return _error("Invalid credentials", 401)

    assert user is not None
    await _limiter().clear(email)
    raw = await service.mint_session(user["user_id"])
    return _session_response(raw, user["user_id"])


@tai42_app.http.custom_route(
    "/api/login/bootstrap",
    methods=["POST"],
    summary="Create the first owner account",
    tags=["login"],
    request_model=BootstrapBody,
    response_model=SessionResponse,
    authed=False,
)
async def login_bootstrap(request: Request) -> Response:
    """Create the first owner under the secure-by-default gate.

    The gate is ON by default via an auto-generated token; ``bootstrap_open``
    disables it (local/dev only). A mismatched/absent token returns a generic 403,
    throttled per IP. On a passing gate the owner row is inserted under an advisory
    lock, its role applied with partial-failure compensation, then a session minted.
    """
    body, error = await _parse(request, BootstrapBody)
    if error is not None:
        return error
    assert body is not None

    too_short = _password_too_short(body.password)
    if too_short is not None:
        return _error(too_short, 422)

    email = service.normalize_email(body.email)
    ip = _client_ip(request)
    settings = accounts_settings()

    if not settings.bootstrap_open:
        effective = await service.resolve_bootstrap_token(service.provider_settings().redis)
        presented = body.bootstrap_token or ""
        if not secrets.compare_digest(presented, effective):
            logger.warning("accounts: bootstrap token mismatch/absent from ip=%s", ip)
            # Throttle the gate brute force per IP (no account exists yet).
            try:
                await _limiter().record_failure(None, ip)
            except RateLimitedError as exc:
                return _throttled_response(exc)
            return _error("Forbidden", 403)

    password_hash = await hash_password_async(body.password)
    row = await service.users_store().create_owner_if_first(
        service.new_user_id(), email, password_hash, service.ADMIN_ROLE
    )
    if row is None:
        return _error("Already initialized", 409)

    owner_id = row["user_id"]
    await service.apply_role_compensated(
        owner_id, service.ADMIN_ROLE, cleanup=lambda: service.users_store().delete(owner_id)
    )
    raw = await service.mint_session(owner_id)
    return _session_response(raw, owner_id)


@tai42_app.http.custom_route(
    "/api/login/invite/accept",
    methods=["POST"],
    summary="Accept an invite and set a password",
    tags=["login"],
    request_model=InviteAcceptBody,
    response_model=SessionResponse,
    authed=False,
)
async def login_invite_accept(request: Request) -> Response:
    """Consume an invite, set the user's first password, and mint a session.

    The invite is consumed atomically (single-use, TTL enforced in the UPDATE); a
    miss returns a generic 400 with the detailed cause server-logged first.
    """
    body, error = await _parse(request, InviteAcceptBody)
    if error is not None:
        return error
    assert body is not None

    ip = _client_ip(request)
    too_short = _password_too_short(body.password)
    if too_short is not None:
        return _error(too_short, 422)
    if body.password != body.password_confirm:
        return _error("Passwords do not match", 422)

    user_id = await service.invites_store().consume(service.token_hash(body.invite_token), _now())
    if user_id is None:
        logger.warning("accounts: invite consume miss (unknown/expired/consumed) from ip=%s", ip)
        # A replayed or guessed invite is a security signal — throttle per IP.
        try:
            await _limiter().record_failure(None, ip)
        except RateLimitedError as exc:
            return _throttled_response(exc)
        return _error("Invalid or expired invite", 400)

    password_hash = await hash_password_async(body.password)
    await service.users_store().set_password_hash(user_id, password_hash)
    raw = await service.mint_session(user_id)
    return _session_response(raw, user_id)


@tai42_app.lifecycle.on_startup
def _assert_accounts_provider_instantiated() -> None:
    """Fail boot loudly if the accounts routes are mounted but the provider was
    never instantiated — access control is disabled and the settings holder is
    empty."""
    if not service.provider_settings_populated():
        raise RuntimeError(
            "tai42-accounts-postgres routes are mounted but its provider was never instantiated — "
            "the accounts kind requires ACCESS_CONTROL_ENABLE=true and 'accounts-postgres' present "
            "in ACCESS_CONTROL_AUTH_PROVIDERS"
        )
