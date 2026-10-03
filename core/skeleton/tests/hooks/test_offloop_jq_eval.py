"""The hooks firing path evaluates its authored jq off the event loop. The condition routes
through the shared ``run_jq_first`` helper; the door-contract ``start_expr`` routes through the
door-contract evaluator (also off-loop). A fire-time jq error still propagates loudly — no site
swallows it."""

from __future__ import annotations

import logging

from tai42_contract.hooks import HookParams
from tai42_contract.template import TemplatedText

import tai42_skeleton.hooks.managers.base_hooks_manager as bhm
from tai42_skeleton.hooks.managers.in_memory_hooks_manager import InMemoryHooksManager
from tai42_skeleton.hooks.settings import HooksSettings


async def test_condition_and_expr_evaluate_through_off_loop_helper(make_app, monkeypatch):
    calls: list[tuple[str, dict]] = []
    real = bhm.run_jq_first

    async def _spy(expression, payload):
        calls.append((expression, payload))
        return await real(expression, payload)

    monkeypatch.setattr(bhm, "run_jq_first", _spy)

    app = make_app()
    manager = InMemoryHooksManager(HooksSettings())
    await manager.register(
        HookParams(
            name="c",
            topic="t",
            tool="noop",
            execution_key="k-fire",
            execution_key_fingerprint="fp-fire",
            condition=TemplatedText(content='.status == "ready"'),
            start_expr=TemplatedText(content="{id: .id}"),
        )
    )

    await manager.on_event("t", {"id": 3, "status": "ready"})

    # The condition runs through the off-loop ``run_jq_first`` helper; the ``start_expr`` runs
    # through the door-contract evaluator, and its transformed result reaching the tool proves it.
    assert app.tools.runs == [("noop", {"id": 3})]
    assert ('.status == "ready"', {"id": 3, "status": "ready"}) in calls


async def test_fire_time_jq_error_is_not_swallowed(make_app, caplog):
    app = make_app()
    manager = InMemoryHooksManager(HooksSettings())
    await manager.register(
        # Compiles at register time, raises at evaluation (string -> number).
        HookParams(
            name="bad",
            topic="t",
            tool="noop",
            execution_key="k-fire",
            execution_key_fingerprint="fp-fire",
            condition=TemplatedText(content=".x | tonumber"),
        )
    )

    with caplog.at_level(logging.ERROR):
        await manager.on_event("t", {"x": "abc"})

    # The condition's evaluation error is the hook's own failure: surfaced loudly in the
    # log (never swallowed as a cleanly-false skip) and the hook does not fire.
    assert app.tools.runs == []
    assert any(rec.levelno == logging.ERROR and "bad" in rec.getMessage() for rec in caplog.records)
