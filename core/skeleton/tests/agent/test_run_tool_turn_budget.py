"""The turn budget over the run tool, and the retained standalone TurnBudgetMiddleware."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from fastmcp.server.middleware import MiddlewareContext
from tai42_kit.utils.detached_util import mark_detached_run, reset_detached_run

from tai42_skeleton.app.instance import app
from tai42_skeleton.exceptions.exceptions import TurnTimeoutError
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools.turn_budget import TurnBudgetMiddleware, _turn_budget_armed, turn_budget

from .conftest import _budget_flag, _fixture_flag, _plain_tools_manifest

# -- deterministic budget expiry against a parked tool ------------------------
# The turn budget arms an ``asyncio.timeout`` the moment a dispatch reaches the shared
# seam, and its deadline runs on the event loop's own clock. A test that asserts the
# expiry landed while the tool was parked on a point (a question, the inner sleep) needs
# the cancellation to be delivered THERE, not in the dispatch that precedes the park —
# but against a real wall clock a loaded host can cross a few-millisecond deadline while
# the task is still in dispatch, delivering the cancellation outside the parked wait.
# These tests take the loop clock under control: freeze it before the dispatch arms the
# budget, let the fixture tool run to its park and signal it, then advance the clock past
# the deadline once — firing the timer with the task provably suspended at the park.


class _ManualClock:
    """A loop-time source that can be frozen and advanced by hand.

    Delegates to the real loop clock until :meth:`freeze` pins it, after which it reports
    ``frozen + offset`` and moves only when :meth:`advance` is called. An armed
    ``asyncio.timeout`` deadline computed while pinned therefore cannot elapse until the
    test advances past it deliberately.
    """

    def __init__(self, real: Callable[[], float]) -> None:
        self._real = real
        self._frozen: float | None = None
        self._offset = 0.0

    def __call__(self) -> float:
        base = self._frozen if self._frozen is not None else self._real()
        return base + self._offset

    def freeze(self) -> None:
        self._frozen = self._real()

    def advance(self, seconds: float) -> None:
        self._offset += seconds


async def _expire_while_parked(coro: Awaitable[Any], parked: asyncio.Event) -> TurnTimeoutError:
    """Run ``coro`` under a frozen loop clock, release the budget deadline once ``parked``
    is set, and return the ``TurnTimeoutError`` the expiry raises.

    Freezing before the dispatch arms the budget holds its deadline no matter how long the
    dispatch takes; the fixture tool sets ``parked`` on reaching its wait, and a single
    advance past the deadline then cancels the turn with the task suspended at that wait.
    """
    loop = asyncio.get_running_loop()
    clock = _ManualClock(loop.time)
    saved_time = loop.time
    loop.time = clock  # type: ignore[method-assign]
    try:
        clock.freeze()
        task = asyncio.ensure_future(coro)
        await parked.wait()
        # Past every armed deadline (0.05s / 0.1s) yet well short of the tool's 5s sleep,
        # so only the budget timer fires and the task is cancelled at its parked wait.
        clock.advance(1.0)
        with pytest.raises(TurnTimeoutError) as excinfo:
            await task
        return excinfo.value
    finally:
        loop.time = saved_time  # type: ignore[method-assign]


# -- the turn budget ----------------------------------------------------------


def _sleeper_manifest() -> Manifest:
    return Manifest.model_validate(
        {"agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": ["sleeper"]}]}
    )


def _sleeper_completed() -> bool:
    return bool(_fixture_flag("tests.agent._fixtures", "sleeper_completed"))


def test_turn_timeout_off_by_default_runs_unbounded():
    # No env set: the timeout is None, the run tool awaits the turn unbounded, and a
    # turn slower than any nonexistent limit still completes.
    async def run() -> None:
        async with app.app_context(_sleeper_manifest()):
            assert await app.tools.run_tool("sleeper", {"seconds": 0.05}) == "done"

    asyncio.run(run())
    assert _sleeper_completed() is True


def test_turn_timeout_set_allows_a_run_under_the_limit(set_turn_timeout):
    set_turn_timeout("5")

    async def run() -> None:
        async with app.app_context(_sleeper_manifest()):
            assert await app.tools.run_tool("sleeper", {"seconds": 0.0}) == "done"

    asyncio.run(run())
    assert _sleeper_completed() is True


def test_turn_timeout_exceeded_raises_typed_error_and_cancels_the_turn(set_turn_timeout):
    set_turn_timeout("0.05")

    async def run() -> None:
        async with app.app_context(_sleeper_manifest()):
            with pytest.raises(TurnTimeoutError, match=r"turn exceeded the 0\.05s turn timeout"):
                await app.tools.run_tool("sleeper", {"seconds": 5})

    asyncio.run(run())
    # The turn was cancelled on expiry, not left running past the deadline.
    assert _sleeper_completed() is False


def test_detached_run_ignores_the_timeout_and_runs_unbounded(set_turn_timeout):
    # A detached run (a background submit or a hook/trigger fire) has no live caller
    # holding a connection, so the turn budget does not apply: with the detached flag
    # set, a turn far slower than the limit still completes and no TurnTimeoutError is
    # raised — exactly as if the setting were unset.
    set_turn_timeout("0.05")

    async def run() -> None:
        async with app.app_context(_sleeper_manifest()):
            token = mark_detached_run()
            try:
                assert await app.tools.run_tool("sleeper", {"seconds": 0.2}) == "done"
            finally:
                reset_detached_run(token)

    asyncio.run(run())
    # The turn ran to its end past the deadline rather than being cancelled on expiry.
    assert _sleeper_completed() is True


def _inner_timeout_manifest() -> Manifest:
    return Manifest.model_validate(
        {"agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": ["inner_timeout"]}]}
    )


def test_inner_timeout_error_passes_through_not_reclassified(set_turn_timeout):
    # A builtin TimeoutError raised by the turn's OWN work (e.g. a jq budget abort)
    # under a generous turn budget reaches the caller unchanged — the wrapper must
    # not mistake it for the turn overrunning its limit.
    set_turn_timeout("300")

    async def run() -> None:
        async with app.app_context(_inner_timeout_manifest()):
            with pytest.raises(TimeoutError, match=r"inner budget exceeded") as excinfo:
                await app.tools.run_tool("inner_timeout", {"seconds": 0.0})
            assert not isinstance(excinfo.value, TurnTimeoutError)

    asyncio.run(run())


def test_plain_tool_over_budget_raises_turn_timeout_and_cancels(set_turn_timeout):
    # A NON-agent tool (a plain function tool) reached through the shared seam is
    # covered by the same budget: over-budget raises the typed error and the run is
    # cancelled on expiry (its completion flag stays False).
    set_turn_timeout("0.05")

    async def run() -> None:
        async with app.app_context(_plain_tools_manifest("slow_tool")):
            with pytest.raises(TurnTimeoutError, match=r"turn exceeded the 0\.05s turn timeout"):
                await app.tools.run_tool("slow_tool", {"seconds": 5})

    asyncio.run(run())
    assert _budget_flag("slow_tool_completed") is False


def test_turn_timeout_names_the_parked_question_on_expiry(set_turn_timeout):
    # A turn killed while parked on an unanswered question names the question (and its
    # interaction id) in the expiry error rather than the bare generic timeout message:
    # the answer wait stamps the pending question on the cancellation that unwinds it,
    # and the wrapper reads it off the cancelled TimeoutError's cause.
    set_turn_timeout("0.05")

    async def run() -> None:
        async with app.app_context(_plain_tools_manifest("parked_question_tool")):
            parked = _budget_flag("parked_question_parked")
            error = await _expire_while_parked(app.tools.run_tool("parked_question_tool", {"seconds": 5}), parked)
            message = str(error)
            assert "turn exceeded the 0.05s turn timeout" in message
            assert "waiting on an unanswered question" in message
            assert "iid-1" in message
            assert "what is the status?" in message

    asyncio.run(run())
    assert _budget_flag("parked_question_completed") is False


def test_turn_timeout_redacts_a_sensitive_parked_question(set_turn_timeout):
    # A turn killed while parked on a SENSITIVE question names the interaction id but
    # NOT the question text — a credential prompt is redacted to ``[sensitive question]``.
    set_turn_timeout("0.05")

    async def run() -> None:
        async with app.app_context(_plain_tools_manifest("parked_sensitive_question_tool")):
            parked = _budget_flag("parked_sensitive_parked")
            error = await _expire_while_parked(
                app.tools.run_tool("parked_sensitive_question_tool", {"seconds": 5}), parked
            )
            message = str(error)
            assert "turn exceeded the 0.05s turn timeout" in message
            assert "waiting on an unanswered question" in message
            assert "iid-9" in message
            assert "[sensitive question]" in message
            assert "what is your password?" not in message

    asyncio.run(run())
    assert _budget_flag("parked_sensitive_completed") is False


def test_turn_timeout_without_a_parked_question_keeps_the_generic_message(set_turn_timeout):
    # An over-budget turn that parked no question (nothing stamped the cancellation)
    # keeps the exact generic expiry message — naming the question is purely additive.
    set_turn_timeout("0.05")

    async def run() -> None:
        async with app.app_context(_plain_tools_manifest("slow_tool")):
            with pytest.raises(TurnTimeoutError) as excinfo:
                await app.tools.run_tool("slow_tool", {"seconds": 5})
            assert str(excinfo.value) == "turn exceeded the 0.05s turn timeout"

    asyncio.run(run())
    assert _budget_flag("slow_tool_completed") is False


def test_plain_tool_under_budget_completes(set_turn_timeout):
    set_turn_timeout("5")

    async def run() -> None:
        async with app.app_context(_plain_tools_manifest("slow_tool")):
            assert await app.tools.run_tool("slow_tool", {"seconds": 0.0}) == "slow-done"

    asyncio.run(run())
    assert _budget_flag("slow_tool_completed") is True


def test_nested_dispatch_bounded_by_outer_window_no_rearm(set_turn_timeout):
    # A nested dispatch through the seam (``nested_outer`` -> ``run_tool`` ->
    # ``nested_inner``) does NOT re-arm a fresh window: the inner call sees the budget
    # already armed by the outer call and opens none of its own, so the single OUTER
    # window bounds the whole turn and cancels the slow inner mid-run.
    set_turn_timeout("0.1")

    async def run() -> None:
        async with app.app_context(_plain_tools_manifest("nested_outer", "nested_inner")):
            parked = _budget_flag("nested_inner_parked")
            error = await _expire_while_parked(app.tools.run_tool("nested_outer", {"seconds": 5}), parked)
            assert "turn exceeded the 0.1s turn timeout" in str(error)

    asyncio.run(run())
    # The inner tool observed the budget already armed (the guard), so it opened no
    # window of its own; the outer window cancelled it before it could complete.
    assert _budget_flag("nested_inner_saw_armed") is True
    assert _budget_flag("nested_inner_completed") is False


def test_nested_turn_budget_reuses_outer_window_and_disarms_cleanly(set_turn_timeout):
    # Directly on the arming primitive: the outer call arms the guard, a nested call
    # sees it armed and adds no arming of its own, exiting the nested block leaves the
    # outer arming intact, and only the outer exit disarms.
    set_turn_timeout("5")

    async def run() -> None:
        assert _turn_budget_armed.get() is False
        async with turn_budget():
            assert _turn_budget_armed.get() is True
            async with turn_budget():
                assert _turn_budget_armed.get() is True
            assert _turn_budget_armed.get() is True
        assert _turn_budget_armed.get() is False

    asyncio.run(run())


def test_turn_budget_unset_runs_unbounded():
    # No env set: the budget is None, so the arming primitive is a pure pass-through —
    # it never arms and a block far slower than any nonexistent limit still completes.
    async def run() -> None:
        async with turn_budget():
            assert _turn_budget_armed.get() is False
            await asyncio.sleep(0.05)

    asyncio.run(run())


# -- the retained standalone TurnBudgetMiddleware -----------------------------
# A public middleware NOT registered by the platform (the live ``tools/call`` edge
# budget is armed by ``DispatchScopeMiddleware``); these exercise its standalone
# behavior and its deprecation warning.


def test_edge_middleware_over_budget_raises_turn_timeout(set_turn_timeout):
    # ``TurnBudgetMiddleware.on_call_tool`` wraps ``call_next`` in the turn budget, so a
    # ``call_next`` slower than the limit is cancelled on expiry and raises the typed
    # error.
    set_turn_timeout("0.05")
    with pytest.warns(DeprecationWarning, match="not registered by the platform"):
        mw = TurnBudgetMiddleware()

    async def call_next(_ctx):
        await asyncio.sleep(5)
        return "reached"

    async def run() -> None:
        with pytest.raises(TurnTimeoutError, match=r"turn exceeded the 0\.05s turn timeout"):
            await mw.on_call_tool(cast("MiddlewareContext[Any]", object()), call_next)

    asyncio.run(run())


def test_edge_middleware_arms_once_nested_run_tool_sees_armed_no_rearm(set_turn_timeout):
    # ``on_call_tool`` arms the budget BEFORE ``call_next`` runs; when ``call_next``
    # reaches the shared ``run_tool`` seam, the seam sees the guard already armed and
    # opens no second window — the outer window bounds the whole turn.
    set_turn_timeout("5")

    async def run() -> None:
        async with app.app_context(_plain_tools_manifest("nested_inner")):
            with pytest.warns(DeprecationWarning, match="not registered by the platform"):
                mw = TurnBudgetMiddleware()

            async def call_next(_ctx):
                # The middleware already armed the budget: the guard is set before the seam.
                assert _turn_budget_armed.get() is True
                return await app.tools.run_tool("nested_inner", {"seconds": 0.0})

            assert await mw.on_call_tool(cast("MiddlewareContext[Any]", object()), call_next) == "inner-done"

    asyncio.run(run())
    # The seam ran under the guard the middleware armed, so it opened no window of its own.
    assert _budget_flag("nested_inner_saw_armed") is True
    assert _budget_flag("nested_inner_completed") is True


def test_turn_budget_middleware_instantiation_warns_it_is_not_registered():
    # The class is a retained standalone component the platform does not register (the
    # live edge budget is DispatchScopeMiddleware's); instantiation warns.
    with pytest.warns(DeprecationWarning, match="not registered by the platform"):
        TurnBudgetMiddleware()
