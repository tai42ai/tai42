"""No authority reader treats an UNBOUND execution identity as privileged.

``rebuild_execution_identity`` can fail and the api-door context then degrades to UNBOUND
(``get_execution_identity() is None``, ``states/api_context.py``). Every reader that keys a
privileged decision on the execution identity fail-closes when it is unbound: it denies,
raises, or resolves to a non-privileged principal — never a fabricated or default-privileged
outcome. These tests pin that present-design invariant across each reader; a future change
that reads an unbound identity as privileged reds one of them.
"""

from __future__ import annotations

from typing import cast

import pytest
from tai42_contract.interactions import reset_resume_continuation_tool, set_resume_continuation_tool

from tai42_skeleton.access_control import user as user_module
from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.app.server import TaiMCP
from tai42_skeleton.authz.check import check
from tai42_skeleton.authz.execution_identity import get_execution_identity
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.interactions.ask.park import resolve_async_continuation
from tai42_skeleton.operations import _authority as authority
from tai42_skeleton.operations.errors import OperationFailedError, PermissionDeniedError
from tai42_skeleton.operations.registry import OperationMetadata


async def test_tool_edge_check_denies_unbound_external_caller() -> None:
    # The tool-edge authorization entry point: with the gate ON, no execution identity bound,
    # and an external caller carrying no resolvable id, the check DENIES fail-closed rather
    # than falling through to any privileged term of the tail.
    assert get_execution_identity() is None
    with pytest.raises(PermissionDeniedError):
        await check(
            CallerIdentity(user_id=None, is_internal=False),
            cast(OperationMetadata, object()),
            {},
            settings=AccessControlSettings(enable=True),
        )


async def test_resolve_caller_raises_when_no_principal_is_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    # The operations authority: gate ON, no execution identity bound, and no request-scope
    # caller id — the acting-principal resolution RAISES the typed failure rather than
    # escalating an unbound dispatch to an admin caller.
    monkeypatch.setattr(authority, "access_control_settings", lambda: AccessControlSettings(enable=True))
    monkeypatch.setattr(authority, "get_current_user_id", lambda: None)
    assert get_execution_identity() is None
    with pytest.raises(OperationFailedError):
        await authority.resolve_caller()


def test_acting_principal_is_not_admin_when_unbound(monkeypatch: pytest.MonkeyPatch) -> None:
    # The isolation seam's acting-principal read: with no execution identity bound it FALLS
    # BACK to the request-scope facts, never fabricating one. With no request principal
    # either, the acting principal is empty and NOT admin.
    monkeypatch.setattr(user_module, "get_current_user_id", lambda: None)
    monkeypatch.setattr(user_module, "get_request_identity_claims", lambda: None)
    monkeypatch.setattr(user_module, "get_request_is_admin", lambda: False)
    assert get_execution_identity() is None
    own, claims, is_admin = user_module._acting_principal()
    assert own is None
    assert claims is None
    assert is_admin is False


async def test_resolve_connection_auth_refuses_with_no_identity() -> None:
    # The connection-credential facade: it reads the execution identity FIRST and refuses
    # before any resolution when none is bound, so an identity-less door never receives the
    # operator's service token.
    assert get_execution_identity() is None
    with pytest.raises(RuntimeError, match="no execution identity bound"):
        await TaiMCP._resolve_connection_auth(cast(TaiMCP, object()), "conn-1", "prov-1", "sub-1")


def test_async_continuation_refuses_with_no_identity() -> None:
    # The async-park resume binding: with a resuming driver bound but no execution identity to
    # rebind the continuation as, it RAISES rather than persisting a question no answer could
    # resume under an authority.
    token = set_resume_continuation_tool("resume_tool")
    try:
        assert get_execution_identity() is None
        with pytest.raises(RuntimeError, match="bound execution identity"):
            resolve_async_continuation()
    finally:
        reset_resume_continuation_tool(token)
