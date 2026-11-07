"""The run's bound: set only inside ``bounded_run()``, ending at its seconds, settling the work owed at its exit."""

from __future__ import annotations

import asyncio
import time

import pytest

from tai42_kit.utils.run_bound import RunBound, bounded_run, run_bound


def test_a_run_is_unbounded_outside_the_scope() -> None:
    assert run_bound() is None


async def test_the_scope_sets_the_run_s_bound_and_resets_it() -> None:
    async with bounded_run(30) as bound:
        assert run_bound() is bound
    assert run_bound() is None


async def test_the_bound_ends_its_seconds_after_the_run_started() -> None:
    before = time.monotonic()
    async with bounded_run(30) as bound:
        after = time.monotonic()
        assert before + 30 <= bound.ends_at <= after + 30
        assert 29 < bound.seconds_left() <= 30


def test_the_seconds_left_of_a_bound_that_passed_are_zero() -> None:
    assert RunBound(-5).seconds_left() == 0.0


async def test_a_scope_inside_a_bounded_run_yields_the_run_s_bound() -> None:
    async with bounded_run(30) as outer:
        async with bounded_run(5) as inner:
            assert inner is outer
            assert run_bound() is outer
        assert run_bound() is outer
    assert run_bound() is None


async def test_the_bound_is_reset_when_the_run_raises() -> None:
    with pytest.raises(RuntimeError):
        async with bounded_run(30):
            raise RuntimeError("boom")
    assert run_bound() is None


async def test_the_owed_work_runs_in_order_at_the_exit_after_the_body_returned() -> None:
    events: list[str] = []

    def owe(name: str) -> None:
        async def work() -> None:
            events.append(f"{name}, bound set: {run_bound() is not None}")

        bound = run_bound()
        assert bound is not None
        bound.once_ended(work)

    async with bounded_run(30):
        owe("first")
        owe("second")
        events.append("body returned")

    assert events == ["body returned", "first, bound set: True", "second, bound set: True"]
    assert run_bound() is None


@pytest.mark.parametrize("raised", [RuntimeError("boom"), asyncio.CancelledError()], ids=["error", "cancelled"])
async def test_the_owed_work_runs_at_the_exit_when_the_body_raised(raised: BaseException) -> None:
    events: list[str] = []

    async def work() -> None:
        events.append("owed work")

    async def run() -> None:
        async with bounded_run(30) as bound:
            bound.once_ended(work)
            raise raised

    with pytest.raises(type(raised)):
        await run()

    assert events == ["owed work"]
    assert run_bound() is None


async def test_owed_work_owed_inside_an_inner_scope_runs_at_the_run_s_exit() -> None:
    events: list[str] = []

    async def work() -> None:
        events.append("owed work")

    async with bounded_run(30):
        async with bounded_run(5) as inner:
            inner.once_ended(work)
        assert events == []
    assert events == ["owed work"]


async def test_a_raise_from_owed_work_propagates_with_the_run_s_error_as_its_context() -> None:
    run_error = RuntimeError("the run failed")
    owed_error = ValueError("the owed work failed")
    after: list[str] = []

    async def failing() -> None:
        raise owed_error

    async def later() -> None:
        after.append("later work")

    async def run() -> None:
        async with bounded_run(30) as bound:
            bound.once_ended(failing)
            bound.once_ended(later)
            raise run_error

    with pytest.raises(ValueError, match="the owed work failed") as raised:
        await run()

    assert raised.value is owed_error
    assert raised.value.__context__ is run_error
    # The work owed after the one that raised still ran.
    assert after == ["later work"]
    assert run_bound() is None


async def test_several_raises_from_owed_work_propagate_together() -> None:
    async def failing() -> None:
        raise ValueError("owed work failed")

    async def run() -> None:
        async with bounded_run(30) as bound:
            bound.once_ended(failing)
            bound.once_ended(failing)

    with pytest.raises(ExceptionGroup) as raised:
        await run()

    assert [type(error) for error in raised.value.exceptions] == [ValueError, ValueError]


async def test_work_owed_to_a_bound_that_settled_raises() -> None:
    # A task created inside the run copies its bound and may outlive the run's exit: work it owes
    # then would never run, so owing it raises.
    async def owed() -> None:
        return None

    async with bounded_run(60) as bound:
        pass

    with pytest.raises(RuntimeError, match="the bounded run has already settled the work it owed at its exit"):
        bound.once_ended(owed)


async def test_work_owed_while_the_bound_settles_raises_from_the_settle() -> None:
    ran: list[str] = []

    async def late() -> None:
        ran.append("late")

    async def owing_more() -> None:
        current = run_bound()
        assert current is not None
        current.once_ended(late)

    with pytest.raises(RuntimeError, match="already settled"):
        async with bounded_run(60) as bound:
            bound.once_ended(owing_more)

    assert ran == []
