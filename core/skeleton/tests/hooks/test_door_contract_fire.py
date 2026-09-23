"""A hook is a parkable-driving door: each fire evaluates its four contract jqs over the event
payload with the run's parked interactions bound as ``$parked``, and drives the shared ``visit``
receiver-less (``receives_outcome=False``) with the resolved cancel / resume / start / extras."""

from __future__ import annotations

import pytest
from tai42_contract.hooks import HookParams
from tai42_contract.interactions import ParkedEntry, TakeItem
from tai42_contract.template import TemplatedText

from tai42_skeleton.hooks.managers.in_memory_hooks_manager import InMemoryHooksManager


def _hook(**exprs) -> HookParams:
    return HookParams(
        name="h",
        topic="t",
        tool="noop",
        execution_key="svc-key",
        execution_key_fingerprint="fp",
        **exprs,
    )


async def test_start_expr_reads_parked_and_reaches_the_tool(make_app) -> None:
    app = make_app()
    app.interactions.parked = [ParkedEntry(id="i-1", status="asking"), ParkedEntry(id="i-2", status="asking")]
    hook = _hook(start_expr=TemplatedText(content="{waiting: ($parked | length)}"))
    await InMemoryHooksManager._run_hook(hook, {"event": 1})
    # The start_expr's $parked-derived kwargs reached the fired tool.
    assert app.tools.runs == [("noop", {"waiting": 2})]
    assert app.interactions.visit_calls[0].receives_outcome is False


async def test_parked_is_bound_with_no_null_keys(make_app) -> None:
    # A parked entry's unset optional fields are ABSENT from ``$parked``, never null-valued keys:
    # the door binds the one compact shape.
    app = make_app()
    app.interactions.parked = [ParkedEntry(id="i-1", status="asking")]
    hook = _hook(
        start_expr=TemplatedText(content="{nulls: [$parked[0] | to_entries[] | select(.value == null) | .key]}")
    )
    await InMemoryHooksManager._run_hook(hook, {"event": 1})
    assert app.tools.runs == [("noop", {"nulls": []})]


async def test_cancel_and_resume_reach_visit_receiver_less(make_app) -> None:
    app = make_app()
    app.interactions.parked = [ParkedEntry(id="i-1", status="asking", to="caller")]
    hook = _hook(
        cancel_expr=TemplatedText(content="[$parked[].id]"),
        resume_expr=TemplatedText(content="$parked[0].id"),
    )
    await InMemoryHooksManager._run_hook(hook, {"event": 1})
    call = app.interactions.visit_calls[0]
    assert call.cancel == ["i-1"]
    assert call.resume == [TakeItem(id="i-1")]
    # A hook is a receiver-less door: a parking target is subject-tracked, never returned inline.
    assert call.receives_outcome is False


async def test_extras_expr_reaches_visit(make_app) -> None:
    app = make_app()
    hook = _hook(extras_expr=TemplatedText(content="{warm: .seed}"))
    await InMemoryHooksManager._run_hook(hook, {"seed": "hi"})
    assert app.interactions.visit_calls[0].extras == {"warm": "hi"}


async def test_null_start_expr_starts_nothing(make_app) -> None:
    app = make_app()
    app.interactions.parked = [ParkedEntry(id="i-1", status="asking", to="caller")]
    hook = _hook(start_expr=TemplatedText(content="null"), cancel_expr=TemplatedText(content="$parked[0].id"))
    await InMemoryHooksManager._run_hook(hook, {"event": 1})
    # start_expr null → nothing started, but the cancel still reaches visit.
    assert app.tools.runs == []
    call = app.interactions.visit_calls[0]
    assert call.started is False
    assert call.cancel == ["i-1"]


def test_validate_jq_fields_refuses_a_bad_door_jq() -> None:
    with pytest.raises(ValueError, match="start_expr is not valid jq"):
        InMemoryHooksManager.validate_jq_fields(_hook(start_expr=TemplatedText(content="this is ( not jq")))


def test_validate_jq_fields_declares_the_parked_variable() -> None:
    # A door jq reading $parked compiles (the variable is declared), where a plain jq compile would
    # fail on the undeclared $name.
    InMemoryHooksManager.validate_jq_fields(_hook(cancel_expr=TemplatedText(content="$parked[0].id")))
