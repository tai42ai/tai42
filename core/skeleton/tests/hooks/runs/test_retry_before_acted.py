"""A failure before the hook acts is retried with backoff; the tool then runs once, keyed by the run id."""

from __future__ import annotations

import pytest
from tai42_contract.hooks import HookSubject
from tai42_contract.template import TemplatedText
from tai42_contract.tools import TOOL_EXTRA_IDEMPOTENCY_KEY

from tai42_skeleton.hooks.runs.sweep import sweep_tick

from .conftest import all_runs, flaky, get_run, held_run, hook, metric, real_hook


async def _next_attempt(hook_runs, clock, seconds: float) -> None:
    clock.advance(seconds)
    await sweep_tick()
    await hook_runs.drain()


async def test_parked_read_faults_are_retried_until_the_tool_runs_once(backend, world, hook_runs, clock) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    world.interactions.parked_faults = [ConnectionError("store down"), ConnectionError("store down")]
    base = manager.settings.run_retry_base_seconds
    failures_before = metric("tai42_hook_runs_attempt_failures_total", error_kind="unavailable")

    result = await manager.on_event("t", {"n": 1})
    [run_id] = result.run_ids
    await hook_runs.drain()

    first = await get_run(manager, run_id)
    assert first is not None
    assert first.status == "pending"
    assert first.attempts == 1
    assert first.acted == ""
    assert first.last_error_kind == "unavailable"
    assert first.next_attempt_at == clock.t + base
    assert world.tools.calls == []

    # Not yet due: a tick just before the backoff ends claims nothing.
    await _next_attempt(hook_runs, clock, base - 1)
    assert (await held_run(manager, run_id)).attempts == 1
    await _next_attempt(hook_runs, clock, 1)

    second = await get_run(manager, run_id)
    assert second is not None
    assert second.status == "pending"
    assert second.attempts == 2
    assert second.acted == ""
    assert second.last_error_kind == "unavailable"
    assert second.next_attempt_at == clock.t + base * 2
    assert world.tools.calls == []

    await _next_attempt(hook_runs, clock, base * 2)

    assert await all_runs(manager) == []
    [call] = world.tools.calls
    assert call.extras[TOOL_EXTRA_IDEMPOTENCY_KEY] == run_id
    assert len(world.interactions.parked_reads) == 3
    assert metric("tai42_hook_runs_attempt_failures_total", error_kind="unavailable") == failures_before + 2


class _FlakyResources:
    """Renders templated text inline, raising ``ConnectionError`` on the first renders of ``flaky``."""

    def __init__(self, flaky: str, faults: int) -> None:
        self.flaky = flaky
        self.faults = faults

    async def render_templated_text(self, text, locale=None):
        if text.content == self.flaky and self.faults:
            self.faults -= 1
            raise ConnectionError("template store down")
        return text.content


async def test_a_state_context_render_fault_is_retried(backend, world, hook_runs, clock) -> None:
    manager = hook_runs.use(backend.manager())
    subject = HookSubject(target_kind="agent", target_name="a", kind="person", key_expr=TemplatedText(content=".who"))
    await manager.register(hook("a", subject=subject))
    world.storage.resource_manager = _FlakyResources(".who", 2)
    base = manager.settings.run_retry_base_seconds

    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    await hook_runs.drain()
    for wait in (base, base * 2):
        run = await get_run(manager, run_id)
        assert run is not None
        assert (run.status, run.acted, run.last_error_kind) == ("pending", "", "unavailable")
        assert world.tools.calls == []
        await _next_attempt(hook_runs, clock, wait)

    assert await all_runs(manager) == []
    [call] = world.tools.calls
    assert call.state_context is not None
    assert call.state_context.candidates.by_kind == {"person": "pA"}
    assert call.extras[TOOL_EXTRA_IDEMPOTENCY_KEY] == run_id


# -- failures inside the real visit, before it acts ----------------------------------

_RESUME = '{id: "i1", payload: {value: 1}}'


def _fault(monkeypatch, point: str) -> list:
    from tai42_skeleton.interactions import visit as visit_module
    from tai42_skeleton.interactions.store import InteractionStore
    from tai42_skeleton.states.outbox import drain as drain_module

    two = [ConnectionError("store down"), ConnectionError("store down")]
    if point == "parked_index":
        return flaky(monkeypatch, visit_module, "_parked_index", two)
    if point == "get_state":
        return flaky(monkeypatch, InteractionStore, "get_state", two)
    if point == "record_answer":
        return flaky(monkeypatch, InteractionStore, "record_answer", two)
    if point == "claim_outcome":
        return flaky(monkeypatch, InteractionStore, "claim_outcome", two)
    if point == "drain":
        calls: list = []
        timeouts = [TimeoutError("drain"), TimeoutError("drain")]

        async def _drain(service, keys, *, deadline, applying):
            calls.append(keys)
            if timeouts:
                raise timeouts.pop(0)

        monkeypatch.setattr(drain_module, "_states_on", lambda: True)
        monkeypatch.setattr(drain_module, "live_states_service", lambda: object())
        monkeypatch.setattr(drain_module, "drain_subjects", _drain)
        return calls
    raise AssertionError(point)


@pytest.mark.parametrize(
    ("point", "kind"),
    [
        ("parked_index", "unavailable"),
        ("drain", "timed_out"),
        ("get_state", "unavailable"),
        ("record_answer", "unavailable"),
    ],
)
async def test_a_resume_hook_fault_before_the_claim_is_retried(
    point, kind, backend, real_world, hook_runs, clock, monkeypatch
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content=_RESUME)))
    await real_world.park("i1")
    calls = _fault(monkeypatch, point)
    base = manager.settings.run_retry_base_seconds
    open_before = metric("tai42_hook_runs_claim_settles_total", result="open")

    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    await hook_runs.drain()
    for wait in (base, base * 2):
        run = await get_run(manager, run_id)
        assert run is not None
        assert (run.status, run.acted, run.last_error_kind, run.claim_intent) == ("pending", "", kind, None)
        assert await real_world.status("i1") == "pending"
        assert not await real_world.due_present("i1")
        await _next_attempt(hook_runs, clock, wait)

    assert await all_runs(manager) == []
    assert await real_world.status("i1") == "answered"
    assert len(calls) == 3
    settled_open = metric("tai42_hook_runs_claim_settles_total", result="open") - open_before
    assert settled_open == (2 if point == "record_answer" else 0)


async def test_a_take_hook_claim_fault_is_retried(backend, real_world, hook_runs, clock, monkeypatch) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content='"cid"')))
    await real_world.outcome("cid", "finished", {"v": 7})
    calls = _fault(monkeypatch, "claim_outcome")
    base = manager.settings.run_retry_base_seconds
    open_before = metric("tai42_hook_runs_claim_settles_total", result="open")

    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    await hook_runs.drain()
    for wait in (base, base * 2):
        run = await get_run(manager, run_id)
        assert run is not None
        assert (run.status, run.acted, run.last_error_kind, run.claim_intent) == ("pending", "", "unavailable", None)
        assert await real_world.outcome_waiting("cid")
        await _next_attempt(hook_runs, clock, wait)

    assert await all_runs(manager) == []
    assert not await real_world.outcome_waiting("cid")
    assert len(calls) == 3
    assert metric("tai42_hook_runs_claim_settles_total", result="open") - open_before == 2


@pytest.mark.parametrize("marker", ["raises", "lease_lost"])
async def test_a_failed_before_marker_leaves_the_claim_uncalled(
    marker, backend, real_world, hook_runs, clock, monkeypatch
) -> None:
    from tai42_skeleton.interactions.store import InteractionStore

    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content=_RESUME)))
    await real_world.park("i1")
    claims = flaky(monkeypatch, InteractionStore, "record_answer", [])
    original = manager._mark_acted
    refused = {"n": 0}

    async def _mark(run_id, claimed_by, act, start_spec):
        if act.action == "resume" and act.phase == "before" and refused["n"] == 0:
            refused["n"] += 1
            if marker == "raises":
                raise ConnectionError("run store down")
            return False
        return await original(run_id, claimed_by, act, start_spec)

    monkeypatch.setattr(manager, "_mark_acted", _mark)

    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    await hook_runs.drain()

    assert claims == []
    run = await get_run(manager, run_id)
    assert run is not None
    assert run.acted == ""
    assert run.claim_intent is None
    assert await real_world.status("i1") == "pending"
    if marker == "raises":
        assert run.status == "pending"
        clock.advance(manager.settings.run_retry_base_seconds)
    else:
        # The attempt stopped recording nothing; its lease lapses and the sweep reclaims it.
        assert run.status == "running"
        clock.advance(manager.settings.run_lease_seconds + 1)
    await sweep_tick()
    await hook_runs.drain()

    assert len(claims) == 1
    assert await all_runs(manager) == []
    assert await real_world.status("i1") == "answered"
