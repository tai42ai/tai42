"""Bind a request-scope caller whose governing role grants a given per-tag level map.

A door test that renders the caller's view of holders binds the caller here. The standing
lookup and the role-grant read are answered in memory; the governing-role, level and
grantable-tag rules the feature reach applies run as they are. :func:`level_route` records a
route a role's level opens under a synthetic feature tag, for a holder kind of a test's own.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.responses import Response
from tai42_contract.access_control import AccessPolicy
from tai42_contract.access_control.context import reset_request_user_id, set_request_user_id

import tai42_skeleton.access_control.feature_reach as feature_reach
import tai42_skeleton.access_control.role_grants as role_grants
from tai42_skeleton.access_control.request_scopes import (
    reset_request_identity_claims,
    reset_request_is_admin,
    set_request_identity_claims,
    set_request_is_admin,
)
from tai42_skeleton.access_control.role_gate import reset_route_index
from tai42_skeleton.access_control.roles import ROLE_POINTER_KEY
from tai42_skeleton.access_control.standing import Standing
from tai42_skeleton.app.route_registry import route_registry

CALLER = "caller-1"
ROLE = "caller-role"


@contextmanager
def bound_caller(
    monkeypatch: pytest.MonkeyPatch, grants: Mapping[str, str] | None = None, *, admin: bool = False
) -> Iterator[None]:
    """Bind :data:`CALLER` for the block: an admin when ``admin``, else a role granting ``grants``."""

    async def version() -> int:
        return 1

    async def standing(_enforcer: object, user_id: str, **_kwargs: object) -> Standing:
        policy = AccessPolicy(policy_data={ROLE_POINTER_KEY: ROLE})
        return Standing(
            principal=user_id, policy=policy, owner=None, owner_policy=None, effective_scopes=[], is_admin=False
        )

    async def role_grants_of(role_name: str, _version: int) -> dict[str, str]:
        assert role_name == ROLE
        return dict(grants or {})

    monkeypatch.setattr(
        feature_reach, "policy_enforcer", lambda _settings: SimpleNamespace(current_policy_version=version)
    )
    monkeypatch.setattr(feature_reach, "resolve_standing", standing)
    monkeypatch.setattr(role_grants, "resolve_role_grants", role_grants_of)
    user = set_request_user_id(CALLER)
    claims = set_request_identity_claims({"sub": CALLER})
    is_admin = set_request_is_admin(admin)
    try:
        yield
    finally:
        reset_request_is_admin(is_admin)
        reset_request_identity_claims(claims)
        reset_request_user_id(user)


async def _never_served(_request: Request) -> Response:
    return Response()


@contextmanager
def level_route(tag: str) -> Iterator[None]:
    """Record one authed ``write`` route under ``tag`` for the block, so a role's level opens ``tag``."""
    path = f"/api/{tag}/probe"
    route_registry.record(
        path=path,
        methods=["POST"],
        name=f"{tag}_probe",
        handler=_never_served,
        summary=f"A probe route under {tag!r}",
        tags=[tag],
        authed=True,
        request_model=None,
        response_model=None,
        no_body_reason="test fixture: response body not under test",
        action="write",
    )
    reset_route_index()
    try:
        yield
    finally:
        del route_registry._routes[path, ("POST",)]
        reset_route_index()
