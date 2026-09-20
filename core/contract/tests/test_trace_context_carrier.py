"""Contract tests for the ambient in-process trace-context deposit seam.

Pin the ContextVar deposit's default (``None``), the set/reset round-trip, the
:func:`ambient_trace_context` context manager's deposit + ``finally`` restore (including
nesting), and the task-copy inheritance a detached nested run relies on — both a flows/agents
driver (writer) and a run-config builder (reader) reach through this one shared channel.
"""

from __future__ import annotations

import asyncio


def test_default_is_none():
    from tai42_contract.monitoring import get_ambient_trace_context

    assert get_ambient_trace_context() is None


def test_set_then_reset_round_trips():
    from tai42_contract.monitoring import (
        TraceContext,
        get_ambient_trace_context,
        reset_ambient_trace_context,
        set_ambient_trace_context,
    )

    ctx = TraceContext(trace_id="trace-1", parent_span_id="span-1")
    token = set_ambient_trace_context(ctx)
    try:
        deposited = get_ambient_trace_context()
        assert deposited is not None
        assert deposited.trace_id == "trace-1"
        assert deposited.parent_span_id == "span-1"
    finally:
        reset_ambient_trace_context(token)
    assert get_ambient_trace_context() is None


def test_context_manager_deposits_and_restores():
    from tai42_contract.monitoring import (
        TraceContext,
        ambient_trace_context,
        get_ambient_trace_context,
    )

    assert get_ambient_trace_context() is None
    with ambient_trace_context(TraceContext(trace_id="outer", parent_span_id=None)):
        outer = get_ambient_trace_context()
        assert outer is not None
        assert outer.trace_id == "outer"
        # Nesting restores the OUTER deposit on inner exit, not None.
        with ambient_trace_context(TraceContext(trace_id="inner", parent_span_id="s")):
            inner = get_ambient_trace_context()
            assert inner is not None
            assert inner.trace_id == "inner"
        restored = get_ambient_trace_context()
        assert restored is not None
        assert restored.trace_id == "outer"
    assert get_ambient_trace_context() is None


def test_deposit_inherited_by_a_task_on_a_copy():
    from tai42_contract.monitoring import (
        TraceContext,
        ambient_trace_context,
        get_ambient_trace_context,
    )

    async def run() -> str | None:
        # A task created INSIDE the block runs on a context copy carrying the deposit, so a
        # detached nested run stays joined to the trace even after the block exits.
        with ambient_trace_context(TraceContext(trace_id="trace-1", parent_span_id=None)):
            task = asyncio.ensure_future(_read_after_yield())
        return await task

    async def _read_after_yield() -> str | None:
        await asyncio.sleep(0)
        ctx = get_ambient_trace_context()
        return ctx.trace_id if ctx is not None else None

    assert asyncio.run(run()) == "trace-1"
