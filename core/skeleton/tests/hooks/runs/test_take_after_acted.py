"""A hook that takes a failed waiting outcome has acted: the take stands and the run is held as failed."""

from __future__ import annotations

import logging

import pytest
from tai42_contract.template import TemplatedText

from tai42_skeleton.hooks.runs.loud import HOOK_RUN_FAILED_EVENT_TOPIC
from tai42_skeleton.operations.errors import ConflictError

from .conftest import get_run, held_run, operator, real_hook

_STORED_FAILURE = {"error": "upstream refused", "code": 502}


async def test_a_failed_outcome_take_is_held_failed_with_the_stored_failure(
    backend, real_world, hook_runs, clock, monkeypatch, notices, caplog
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content='"cid"')))
    await real_world.outcome("cid", "failed", _STORED_FAILURE)
    failed_events: list[dict] = []
    on_event = manager.on_event

    async def _on_event(topic, payload, **kwargs):
        if topic == HOOK_RUN_FAILED_EVENT_TOPIC:
            failed_events.append(payload)
        return await on_event(topic, payload, **kwargs)

    monkeypatch.setattr(manager, "on_event", _on_event)
    caplog.set_level(logging.ERROR)

    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    await hook_runs.drain()

    assert not await real_world.outcome_waiting("cid")
    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.attempts, run.acted, run.acted_ids, run.claim_intent) == (
        "failed",
        1,
        "take",
        ("cid",),
        None,
    )
    assert run.last_error_kind == "upstream_error"
    assert repr(_STORED_FAILURE) in (run.last_error or "")
    [logged] = [r for r in caplog.records if r.levelno == logging.ERROR and run_id in r.getMessage()]
    assert repr(_STORED_FAILURE) in logged.getMessage()
    [notice] = [n for n in notices if run_id in n]
    assert repr(_STORED_FAILURE) in notice
    [event] = failed_events
    assert (event["run_id"], event["acted"], event["acted_ids"], event["error_kind"]) == (
        run_id,
        "take",
        ["cid"],
        "upstream_error",
    )
    assert repr(_STORED_FAILURE) in event["error"]

    ops = operator(monkeypatch, manager)
    with pytest.raises(ConflictError, match="take"):
        await ops.retry_hook_run(run_id)
    assert (await held_run(manager, run_id)).status == "failed"
    assert await ops.discard_hook_run(run_id) == {"discarded": run_id}
    assert await get_run(manager, run_id) is None
