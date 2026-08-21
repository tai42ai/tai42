"""Callback glue: rendering, condition gating, and kwarg preparation."""

from __future__ import annotations

import time

import pytest
from tai42_contract.access_control import caller_may_read_secrets
from tai42_kit.backend import CallbackSchema, callback_execution, prepare_backend_kwargs
from tai42_kit.settings.cache_registry import reset_all_settings
from tai42_kit.utils.data import jq_util
from tai42_kit.utils.detached_util import in_detached_run


async def test_prepare_backend_kwargs_injects_tool_name_and_stamps_capability() -> None:
    def some_tool(a: int, b: str = "x") -> None: ...

    # No request bound, so the stamped capability is the fail-closed default False;
    # the worker pops it before running the tool.
    kwargs = await prepare_backend_kwargs(some_tool, "backend_tool_name", "some_tool", {"a": 1})
    assert kwargs == {"a": 1, "backend_tool_name": "some_tool", "backend_secret_capability": False}


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
    assert stub_app.tools.offloads == [True]
    assert in_detached_run() is False


@pytest.mark.parametrize(("gate_enabled", "capable"), [(False, True), (True, False)])
async def test_callback_binds_the_worker_secret_capability(
    stub_app, access_control, gate_enabled: bool, capable: bool
) -> None:
    # A dequeued callback's follow-up tool sees the same worker-bound capability as
    # a dequeued task: OFF -> secret-capable, ON -> fail-closed, reset after.
    access_control(gate_enabled)
    stub_app.tools.run_tool_result = {"ran": True}
    callback = CallbackSchema(expr="{payload: .}", tool="follow_up")

    await callback_execution(7, callback)

    assert stub_app.tools.secret_capability_seen == [capable]
    assert caller_may_read_secrets() is False


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


async def test_callback_jq_eval_is_timeout_bounded(stub_app, monkeypatch) -> None:
    # The callback path evaluates jq through ``run_jq_first``, so a slow program
    # is aborted by JQ_TIMEOUT_SECONDS and the named TimeoutError is raised.
    class _SlowProgram:
        def input(self, payload):
            return self

        def first(self):
            time.sleep(1)
            return None

    monkeypatch.setattr(jq_util, "get_compiled_jq", lambda expr: _SlowProgram())
    monkeypatch.setenv("JQ_TIMEOUT_SECONDS", "0.01")
    reset_all_settings()
    try:
        callback = CallbackSchema(condition=". > 1", expr=". * 2")
        start = time.monotonic()
        with pytest.raises(TimeoutError, match="JQ_TIMEOUT_SECONDS"):
            await callback_execution(3, callback)
        assert time.monotonic() - start < 0.5
    finally:
        reset_all_settings()
