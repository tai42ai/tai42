"""A hook that declares a ``subject`` fires inside a ``hook``-door state context, so a
state write during the fire is keyed and attributed to the hook; a ``key_expr`` that does
not yield a non-empty string fails the fire loudly, never a silent skip."""

from __future__ import annotations

import pytest
from tai42_contract.hooks import HookParams, HookSubject
from tai42_contract.states import StateContext

from tai42_skeleton.hooks.managers.in_memory_hooks_manager import InMemoryHooksManager
from tai42_skeleton.hooks.settings import HooksSettings
from tai42_skeleton.states.context import current_state_context


def _capture_context(app) -> list[StateContext | None]:
    """Wrap the fake tool runner so each dispatch records the ambient state context seen
    at the moment the tool runs — what the hook fire actually establishes."""
    seen: list[StateContext | None] = []
    original = app.tools.run_tool

    async def recording_run_tool(name, tool_input, *, offload_sync=False):
        seen.append(current_state_context())
        return await original(name, tool_input, offload_sync=offload_sync)

    app.tools.run_tool = recording_run_tool
    return seen


def _hook(subject: HookSubject | None) -> HookParams:
    return HookParams(
        name="h",
        topic="t",
        tool="noop",
        execution_key="svc-key",
        execution_key_fingerprint="fp",
        subject=subject,
    )


async def test_declared_subject_establishes_the_hook_door_context(make_app) -> None:
    app = make_app()
    seen = _capture_context(app)
    hook = _hook(HookSubject(target_kind="tool", target_name="assistant", kind="person", key_expr=".actor"))
    await InMemoryHooksManager._run_hook(hook, {"actor": "p-42"})
    assert len(seen) == 1
    ctx = seen[0]
    assert ctx is not None
    assert ctx.door == "hook"
    assert ctx.actor == "svc-key"
    assert ctx.turn_id is None
    assert ctx.candidates.target_kind == "tool"
    assert ctx.candidates.target_name == "assistant"
    assert ctx.candidates.by_kind == {"person": "p-42"}
    # The context is torn down after the fire.
    assert current_state_context() is None


async def test_no_subject_leaves_no_state_context(make_app) -> None:
    app = make_app()
    seen = _capture_context(app)
    await InMemoryHooksManager._run_hook(_hook(None), {"actor": "p-42"})
    assert seen == [None]


@pytest.mark.parametrize("payload", [{"actor": ""}, {"actor": None}, {}, {"actor": 7}])
async def test_key_expr_that_is_not_a_nonempty_string_fails_the_fire(make_app, payload) -> None:
    app = make_app()
    seen = _capture_context(app)
    hook = _hook(HookSubject(target_kind="tool", target_name="assistant", kind="person", key_expr=".actor"))
    with pytest.raises(ValueError, match="must yield a non-empty string"):
        await InMemoryHooksManager._run_hook(hook, payload)
    # The fire failed before running the tool — never a silent skip.
    assert seen == []


def _manager() -> InMemoryHooksManager:
    return InMemoryHooksManager(HooksSettings())
