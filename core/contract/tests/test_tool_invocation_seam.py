"""Contract tests for the ambient in-process invoked-tool seam.

Pin the ContextVar deposit's default (``None``), the set/reset round-trip, the
nested set/reset token discipline (an inner deposit restores the OUTER value, not
``None``), the frozen ``ToolInvocation`` model, and the copy-on-task inheritance —
since the skeleton run-tool binding + the MCP middleware (writers) and any
in-process reader rely on this one shared, logic-free channel.
"""

from __future__ import annotations

import asyncio

import pytest


def test_default_is_none_outside_any_tool():
    from tai42_contract.tools import current_tool_invocation

    assert current_tool_invocation() is None


def test_set_then_reset_round_trips():
    from tai42_contract.tools import (
        ToolInvocation,
        current_tool_invocation,
        reset_current_tool_invocation,
        set_current_tool_invocation,
    )

    token = set_current_tool_invocation(ToolInvocation(tool_name="acme_echo"))
    try:
        inv = current_tool_invocation()
        assert inv is not None
        assert inv.tool_name == "acme_echo"
    finally:
        reset_current_tool_invocation(token)
    assert current_tool_invocation() is None


def test_nested_deposit_restores_outer_on_reset():
    # Token discipline: an inner deposit (a tool invoking another) re-sets for the
    # inner call and restores the OUTER invocation on reset — never ``None``.
    from tai42_contract.tools import (
        ToolInvocation,
        current_tool_invocation,
        reset_current_tool_invocation,
        set_current_tool_invocation,
    )

    outer = set_current_tool_invocation(ToolInvocation(tool_name="outer"))
    try:
        outer_inv = current_tool_invocation()
        assert outer_inv is not None
        assert outer_inv.tool_name == "outer"
        inner = set_current_tool_invocation(ToolInvocation(tool_name="inner"))
        try:
            inner_inv = current_tool_invocation()
            assert inner_inv is not None
            assert inner_inv.tool_name == "inner"
        finally:
            reset_current_tool_invocation(inner)
        # Inner reset restored the outer deposit, not the None default.
        restored = current_tool_invocation()
        assert restored is not None
        assert restored.tool_name == "outer"
    finally:
        reset_current_tool_invocation(outer)
    assert current_tool_invocation() is None


def test_tool_invocation_is_frozen():
    # A deposited invocation is a fact of the active execution, never mutated in place.
    from pydantic import ValidationError

    from tai42_contract.tools import ToolInvocation

    inv = ToolInvocation(tool_name="acme_echo")
    with pytest.raises(ValidationError):
        inv.tool_name = "other"


def test_deposit_inherited_by_a_task_on_a_copy():
    from tai42_contract.tools import (
        ToolInvocation,
        current_tool_invocation,
        reset_current_tool_invocation,
        set_current_tool_invocation,
    )

    async def run() -> str | None:
        # A task created INSIDE the deposited block runs on a context copy carrying the
        # deposit, so a detached continuation stays attributed to the invoked tool even
        # after the depositing block unwinds.
        token = set_current_tool_invocation(ToolInvocation(tool_name="acme_echo"))
        try:
            task = asyncio.ensure_future(_read_after_yield())
        finally:
            reset_current_tool_invocation(token)
        return await task

    async def _read_after_yield() -> str | None:
        await asyncio.sleep(0)
        inv = current_tool_invocation()
        return inv.tool_name if inv is not None else None

    assert asyncio.run(run()) == "acme_echo"
