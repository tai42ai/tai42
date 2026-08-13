"""Callback glue: rendering, condition gating, and kwarg preparation."""

from __future__ import annotations

from tai42_kit.utils.detached_util import in_detached_run

from tai42_backend_celery.core.callback import CallbackSchema, callback_execution, prepare_backend_kwargs


async def test_prepare_backend_kwargs_injects_tool_name() -> None:
    def some_tool(a: int, b: str = "x") -> None: ...

    kwargs = await prepare_backend_kwargs(some_tool, "backend_tool_name", "some_tool", {"a": 1})
    assert kwargs == {"a": 1, "backend_tool_name": "some_tool"}


async def test_rendered_fields_go_through_resource_manager() -> None:
    callback = CallbackSchema(condition_id="cond-1", expr_id="expr-1")
    assert await callback.rendered_condition() == "rendered:cond-1"
    assert await callback.rendered_expr() == "rendered:expr-1"


async def test_condition_failure_returns_none(stub_app) -> None:
    callback = CallbackSchema(condition=".k == 2", expr=".", tool="follow_up")
    result = await callback_execution({"k": 1}, callback)
    assert result is None
    assert stub_app.tools.run_tool_calls == []


async def test_empty_condition_passes_and_runs_tool(stub_app) -> None:
    stub_app.tools.run_tool_result = {"ran": True}
    callback = CallbackSchema(expr="{payload: .}", tool="follow_up")
    result = await callback_execution(7, callback)
    assert result == {"ran": True}
    assert stub_app.tools.run_tool_calls == [("follow_up", {"payload": 7})]


async def test_callback_runs_tool_detached(stub_app) -> None:
    # A worker execution has no live caller, so the callback's tool observes the
    # detached flag set; the flag never leaks past the callback.
    stub_app.tools.run_tool_result = {"ran": True}
    callback = CallbackSchema(expr="{payload: .}", tool="follow_up")

    await callback_execution(7, callback)

    assert stub_app.tools.detached_seen == [True]
    assert in_detached_run() is False


async def test_no_tool_returns_expression_output(stub_app) -> None:
    callback = CallbackSchema(condition=". > 1", expr=". * 2")
    result = await callback_execution(3, callback)
    assert result == 6
    assert stub_app.tools.run_tool_calls == []


async def test_empty_expr_yields_empty_mapping(stub_app) -> None:
    stub_app.tools.run_tool_result = "done"
    callback = CallbackSchema(tool="follow_up")
    result = await callback_execution(3, callback)
    assert result == "done"
    assert stub_app.tools.run_tool_calls == [("follow_up", {})]
