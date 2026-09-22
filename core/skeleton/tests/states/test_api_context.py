"""The ``door="api"`` state context and the opportunistic caller-identity bind the direct doors share.

The synchronous run-tool door, the background submit run, the MCP ``tools/call`` edge and the agent-run
SSE doors all deposit the same ``door="api"`` :class:`StateContext` for a named subject and
opportunistically bind the caller's own execution identity, so an async park indexes under the caller's
subject and a parking tool can rebind its continuation. Homed once here so the four doors cannot drift.
"""

from __future__ import annotations

from tai42_contract.states import StateSubject

from tai42_skeleton.states.api_context import api_state_context, caller_execution_identity
from tai42_skeleton.states.context import current_state_context


def test_api_state_context_deposits_the_named_subject() -> None:
    subject = StateSubject(target_kind="tool", target_name="n", kind="thread", key="k")
    with api_state_context(subject, actor="alice"):
        ctx = current_state_context()
        assert ctx is not None
        assert ctx.door == "api"
        assert ctx.actor == "alice"
        assert ctx.candidates.target_kind == "tool"
        assert ctx.candidates.target_name == "n"
        assert dict(ctx.candidates.by_kind) == {"thread": "k"}
    assert current_state_context() is None


def test_api_state_context_without_a_subject_deposits_nothing() -> None:
    with api_state_context(None, actor="alice"):
        assert current_state_context() is None


async def test_caller_execution_identity_binds_and_resets(monkeypatch) -> None:
    from tai42_skeleton.authz import execution
    from tai42_skeleton.authz.execution_identity import get_execution_identity

    sentinel = object()

    async def _fake_rebuild(key: str):
        assert key == "alice"
        return sentinel

    monkeypatch.setattr(execution, "rebuild_execution_identity", _fake_rebuild)
    assert get_execution_identity() is None
    async with caller_execution_identity("alice"):
        assert get_execution_identity() is sentinel
    assert get_execution_identity() is None


async def test_caller_execution_identity_no_key_binds_nothing(monkeypatch) -> None:
    from tai42_skeleton.authz.execution_identity import get_execution_identity

    async with caller_execution_identity(None):
        assert get_execution_identity() is None


async def test_caller_execution_identity_does_not_clobber_an_existing_bind(monkeypatch) -> None:
    from tai42_skeleton.authz.execution_identity import (
        get_execution_identity,
        reset_execution_identity,
        set_execution_identity,
    )

    existing = object()
    token = set_execution_identity(existing)  # type: ignore[arg-type]
    try:
        async with caller_execution_identity("alice"):
            # An identity already bound is never rebuilt or replaced.
            assert get_execution_identity() is existing
    finally:
        reset_execution_identity(token)


async def test_caller_execution_identity_degrades_when_rebuild_raises(monkeypatch) -> None:
    # A rebuild the infrastructure cannot answer degrades to the pre-bind behavior (unbound, the
    # parking seam fail-closes loudly), logged — never a failed call.
    from tai42_skeleton.authz import execution
    from tai42_skeleton.authz.execution_identity import get_execution_identity

    async def _boom(key: str):
        raise RuntimeError("infra down")

    monkeypatch.setattr(execution, "rebuild_execution_identity", _boom)
    async with caller_execution_identity("alice"):
        assert get_execution_identity() is None
    assert get_execution_identity() is None
