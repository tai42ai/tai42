"""Callback rendering and execution semantics."""

from __future__ import annotations

import pytest
from fastmcp import Context

from tai42_backend_rq.callback import CallbackSchema, callback_execution, prepare_backend_kwargs


async def test_rendered_fields_default_to_empty():
    callback = CallbackSchema(tool="t")
    assert await callback.rendered_condition() == ""
    assert await callback.rendered_expr() == ""


async def test_rendered_fields_use_inline_content(app):
    callback = CallbackSchema(tool="t", condition=". > 5", expr="{value: .}")
    assert await callback.rendered_condition() == ". > 5"
    assert await callback.rendered_expr() == "{value: .}"


async def test_rendered_fields_resolve_template_ids(app):
    app.storage.resource_manager.templates["cond-1"] = ". != null"
    callback = CallbackSchema(tool="t", condition_id="cond-1")
    assert await callback.rendered_condition() == ". != null"


async def test_callback_execution_condition_failure_returns_none(app):
    callback = CallbackSchema(tool="next", condition=". > 5")
    assert await callback_execution(3, callback) is None
    assert app.tools.run_calls == []


async def test_callback_execution_runs_tool_with_expr_output(app):
    app.tools.run_result = "chained-result"
    callback = CallbackSchema(tool="next", condition=". > 5", expr="{value: .}")

    result = await callback_execution(10, callback)
    assert result == "chained-result"
    assert app.tools.run_calls == [("next", {"value": 10})]


async def test_callback_execution_without_condition_always_runs(app):
    app.tools.run_result = "ok"
    callback = CallbackSchema(tool="next", expr="{value: .}")
    assert await callback_execution(1, callback) == "ok"


async def test_callback_execution_without_tool_returns_expr_output(app):
    callback = CallbackSchema(expr="{doubled: (. * 2)}")
    assert await callback_execution(4, callback) == {"doubled": 8}
    assert app.tools.run_calls == []


async def test_callback_execution_without_expr_passes_empty_kwargs(app):
    app.tools.run_result = "ran"
    callback = CallbackSchema(tool="next")
    assert await callback_execution({"anything": 1}, callback) == "ran"
    assert app.tools.run_calls == [("next", {})]


async def test_prepare_backend_kwargs_injects_tool_name():
    async def sample(x: int) -> int:
        return x

    kwargs = await prepare_backend_kwargs(sample, "backend_tool_name", "sample", {"x": 1})
    assert kwargs == {"x": 1, "backend_tool_name": "sample"}


async def test_prepare_backend_kwargs_strips_fastmcp_context():
    async def sample(x: int, ctx: Context) -> int:
        return x

    kwargs = await prepare_backend_kwargs(sample, "backend_tool_name", "sample", {"x": 1, "ctx": object()})
    assert kwargs == {"x": 1, "backend_tool_name": "sample"}


async def test_callback_schema_round_trips_through_validation():
    original = CallbackSchema(tool="t", condition=". > 1", expr="{a: .}")
    restored = CallbackSchema.model_validate(original.model_dump())
    assert restored == original


def test_bare_condition_syntax_error_propagates():
    """A broken jq filter raises loudly instead of silently passing."""
    import asyncio

    callback = CallbackSchema(tool="t", condition="((broken")
    with pytest.raises(ValueError, match="syntax error"):
        asyncio.run(callback_execution(1, callback))
