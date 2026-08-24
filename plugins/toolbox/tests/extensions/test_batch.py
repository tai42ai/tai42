"""Tests for the ``batch`` tool extension."""

import asyncio
import inspect
from datetime import UTC, datetime
from typing import Any

import pytest
from tai42_contract.extensions import ExtensionKind
from tai42_contract.interactions import SuspendedInteraction

import tai42_toolbox._internal.extensions.batch_executor as batch_executor
import tai42_toolbox.extensions.batch as batch_module
from tai42_toolbox._internal.extensions.batch_executor import BatchMultiParkUnsupported, BatchSettings, execute_batch
from tai42_toolbox.extensions.batch import batch

from .conftest import FakeTools


def _tool(n: int) -> int:
    return n


def test_registers_as_transformer_named_batch(capture_registration):
    assert capture_registration(batch_module) == [("batch", ExtensionKind.TRANSFORMER, False)]


def test_composed_signature_is_concrete():
    params = list(inspect.signature(batch(_tool, "tool", "desc")).parameters.values())
    var_kinds = {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}

    assert params
    # A concrete makefun signature, never a bare ``(*args, **kwargs)`` — this is
    # what the apply site's transformer enforcement requires.
    assert not all(p.kind in var_kinds for p in params)
    assert {p.name for p in params} == {"params", "execution_mode", "max_concurrent", "fail_fast"}


def _bind_counting_app(bind_fake_app) -> None:
    async def run_tool(key: str, arguments: dict[str, Any]) -> Any:
        if arguments.get("fail"):
            raise RuntimeError("boom")
        return arguments["n"]

    bind_fake_app(FakeTools(run_tool=run_tool))


def test_sequential_returns_results_in_order(bind_fake_app):
    _bind_counting_app(bind_fake_app)
    params = [{"n": 1}, {"n": 2}, {"n": 3}]
    results = asyncio.run(execute_batch("tool", params, execution_mode="sequential"))
    assert results == [1, 2, 3]


def test_parallel_returns_results_in_order(bind_fake_app):
    _bind_counting_app(bind_fake_app)
    params = [{"n": 1}, {"n": 2}, {"n": 3}]
    results = asyncio.run(execute_batch("tool", params, execution_mode="parallel", max_concurrent=2))
    assert results == [1, 2, 3]


def test_failing_item_raises_loudly(bind_fake_app):
    _bind_counting_app(bind_fake_app)
    params = [{"n": 1}, {"fail": True, "n": 2}, {"n": 3}]
    # fail_fast defaults True: a failing item raises loudly rather than returning
    # a short or None-padded list.
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(execute_batch("tool", params, execution_mode="sequential"))


def test_invalid_mode_raises(bind_fake_app):
    _bind_counting_app(bind_fake_app)
    with pytest.raises(RuntimeError, match="Unknown execution mode"):
        asyncio.run(execute_batch("tool", [], execution_mode="invalid"))  # type: ignore[arg-type]


def _bind_parking_app(bind_fake_app) -> None:
    # A body that carries ``park`` async-parks: its run_tool returns the SuspendedInteraction
    # SIGNAL (as a real parking tool-face does) rather than an ordinary result.
    async def run_tool(key: str, arguments: dict[str, Any]) -> Any:
        if arguments.get("park"):
            return SuspendedInteraction(interaction_id=arguments["park"], expiry_at=datetime(2026, 6, 1, tzinfo=UTC))
        return arguments["n"]

    bind_fake_app(FakeTools(run_tool=run_tool))


@pytest.mark.parametrize("execution_mode", ["sequential", "parallel"])
def test_a_single_parked_body_propagates_the_sentinel(bind_fake_app, execution_mode):
    # One body parked: the batch parks AS A WHOLE, re-surfacing the sentinel verbatim so the
    # caller's park recognition fires — not a partial result list burying a hidden pause.
    _bind_parking_app(bind_fake_app)
    params = [{"n": 1}, {"park": "i-batch"}, {"n": 3}]
    result = asyncio.run(execute_batch("tool", params, execution_mode=execution_mode))
    assert isinstance(result, SuspendedInteraction)
    assert result.interaction_id == "i-batch"


@pytest.mark.parametrize("execution_mode", ["sequential", "parallel"])
def test_two_parked_bodies_raise_multi_park_unsupported(bind_fake_app, execution_mode):
    # A batch is ONE tool call and surfaces ONE park signal: two-plus parks are unsupported and
    # raise loudly, naming the parked interactions, rather than silently corrupting.
    _bind_parking_app(bind_fake_app)
    params = [{"park": "i-a"}, {"n": 2}, {"park": "i-b"}]
    with pytest.raises(BatchMultiParkUnsupported) as excinfo:
        asyncio.run(execute_batch("tool", params, execution_mode=execution_mode))
    assert excinfo.value.interaction_ids == ["i-a", "i-b"]
    assert excinfo.value.tool_name == "tool"


def test_error_slots_never_trip_the_multi_park_guard(bind_fake_app):
    # The guard keys on the SENTINEL count only: errored bodies (error strings under
    # fail_fast=False) plus a single park propagate the ONE park, never a false multi-park.
    async def run_tool(key: str, arguments: dict[str, Any]) -> Any:
        if arguments.get("park"):
            return SuspendedInteraction(interaction_id=arguments["park"])
        if arguments.get("fail"):
            raise RuntimeError("boom")
        return arguments["n"]

    bind_fake_app(FakeTools(run_tool=run_tool))
    params = [{"fail": True, "n": 1}, {"park": "i-solo"}, {"fail": True, "n": 3}]
    result = asyncio.run(execute_batch("tool", params, execution_mode="sequential", fail_fast=False))
    assert isinstance(result, SuspendedInteraction)
    assert result.interaction_id == "i-solo"


@pytest.mark.parametrize("execution_mode", ["sequential", "parallel"])
def test_fail_fast_false_fills_the_slot_with_the_error(bind_fake_app, execution_mode):
    # With fail_fast off, a failing item's error string takes its slot, keeping the list length and order.
    _bind_counting_app(bind_fake_app)
    params = [{"n": 1}, {"fail": True, "n": 2}, {"n": 3}]
    results = asyncio.run(execute_batch("tool", params, execution_mode=execution_mode, fail_fast=False))
    assert results == [1, "boom", 3]


def test_parallel_rejects_non_positive_concurrency(bind_fake_app):
    # A concurrency cap below 1 is a caller error that must raise loudly, never
    # silently run unbounded or do nothing.
    _bind_counting_app(bind_fake_app)
    with pytest.raises(ValueError, match="positive integer"):
        asyncio.run(execute_batch("tool", [{"n": 1}], execution_mode="parallel", max_concurrent=0))


@pytest.mark.parametrize("execution_mode", ["sequential", "parallel"])
def test_over_limit_params_raises_naming_env_var(bind_fake_app, monkeypatch, execution_mode):
    # An over-cap param list is refused loudly in both modes before any tool runs, naming the env var.
    _bind_counting_app(bind_fake_app)
    monkeypatch.setattr(batch_executor, "batch_settings", lambda: BatchSettings(max_batch_size=2))
    params = [{"n": 1}, {"n": 2}, {"n": 3}]
    with pytest.raises(ValueError, match="BATCH_MAX_BATCH_SIZE"):
        asyncio.run(execute_batch("tool", params, execution_mode=execution_mode))


def test_unset_max_concurrent_never_exceeds_default(bind_fake_app, monkeypatch):
    # A parallel batch with no cap runs at most ``default_max_concurrent`` items, never a full fan-out.
    monkeypatch.setattr(
        batch_executor, "batch_settings", lambda: BatchSettings(default_max_concurrent=3, max_batch_size=100)
    )
    cap = 3
    state = {"current": 0, "max": 0}
    gate = asyncio.Event()

    async def run_tool(key: str, arguments: dict[str, Any]) -> Any:
        state["current"] += 1
        state["max"] = max(state["max"], state["current"])
        # Once the semaphore has admitted a full cap's worth, release everyone;
        # under a correct bound exactly ``cap`` hold the semaphore at a time.
        if state["current"] >= cap:
            gate.set()
        await gate.wait()
        state["current"] -= 1
        return arguments["n"]

    bind_fake_app(FakeTools(run_tool=run_tool))
    params = [{"n": i} for i in range(6)]

    async def main() -> list[Any]:
        return await asyncio.wait_for(execute_batch("tool", params, execution_mode="parallel"), timeout=5)

    results = asyncio.run(main())
    assert results == [0, 1, 2, 3, 4, 5]
    assert state["max"] == cap


def test_explicit_max_concurrent_wins_over_default(bind_fake_app, monkeypatch):
    # An explicit ``max_concurrent`` overrides the default: with the default
    # forced to 1, an explicit cap of 3 still runs three items concurrently.
    monkeypatch.setattr(
        batch_executor, "batch_settings", lambda: BatchSettings(default_max_concurrent=1, max_batch_size=100)
    )
    cap = 3
    state = {"current": 0, "max": 0}
    gate = asyncio.Event()

    async def run_tool(key: str, arguments: dict[str, Any]) -> Any:
        state["current"] += 1
        state["max"] = max(state["max"], state["current"])
        if state["current"] >= cap:
            gate.set()
        await gate.wait()
        state["current"] -= 1
        return arguments["n"]

    bind_fake_app(FakeTools(run_tool=run_tool))
    params = [{"n": i} for i in range(3)]

    async def main() -> list[Any]:
        return await asyncio.wait_for(
            execute_batch("tool", params, execution_mode="parallel", max_concurrent=3), timeout=5
        )

    results = asyncio.run(main())
    assert results == [0, 1, 2]
    # All three overlapped — the explicit cap won over the default of 1 (which
    # would have serialized them and never let ``current`` reach 3).
    assert state["max"] == cap


def test_parallel_empty_batch_returns_empty(bind_fake_app):
    # An empty parallel batch with no caller-supplied cap returns [] just as the
    # sequential path does — the default cap must not misfire the concurrency guard.
    _bind_counting_app(bind_fake_app)
    assert asyncio.run(execute_batch("tool", [], execution_mode="parallel")) == []


def test_parallel_fail_fast_cancels_in_flight_siblings(bind_fake_app):
    # Parallel + fail_fast=True: the first failure raises the original exception and any in-flight
    # sibling is cancelled rather than orphaned. Deterministic via events.
    fail_gate = asyncio.Event()
    sibling_running = asyncio.Event()
    sibling_outcome: dict[str, str] = {}

    async def run_tool(key: str, arguments: dict[str, Any]) -> Any:
        if arguments.get("sibling"):
            sibling_running.set()
            try:
                # Wait on a gate that is never set: the only way out is cancellation
                # once the failing sibling raises.
                await fail_gate.wait()
            except asyncio.CancelledError:
                sibling_outcome["state"] = "CANCELLED"
                raise
            sibling_outcome["state"] = "COMPLETED"
            return arguments["n"]
        if arguments.get("fail"):
            # Hold the failure until the sibling is confirmed running, so the
            # cancellation path is the one exercised.
            await sibling_running.wait()
            raise RuntimeError("boom")
        return arguments["n"]

    bind_fake_app(FakeTools(run_tool=run_tool))

    params = [{"sibling": True, "n": 1}, {"fail": True, "n": 2}]

    async def main() -> None:
        with pytest.raises(RuntimeError, match="boom"):
            await execute_batch("tool", params, execution_mode="parallel")
        # Asserted inside the running loop: the executor must cancel the sibling before re-raising
        # (loop teardown would otherwise cancel a leftover task and mask a missing fix).
        assert sibling_outcome.get("state") == "CANCELLED"

    asyncio.run(main())
