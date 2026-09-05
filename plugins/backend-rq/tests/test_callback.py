"""Callback rendering and execution semantics."""

from __future__ import annotations

import time

import pytest
from fastmcp import Context
from tai42_contract.access_control import caller_may_read_secrets
from tai42_kit.backend import CallbackSchema, callback_execution, prepare_backend_kwargs
from tai42_kit.settings.cache_registry import reset_all_settings
from tai42_kit.utils.data import jq_util
from tai42_kit.utils.detached_util import in_detached_run


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


async def test_callback_execution_runs_tool_detached(app):
    # A worker execution has no live caller, so the callback's tool observes the
    # detached flag set; the flag never leaks past the callback.
    app.tools.run_result = "chained-result"
    callback = CallbackSchema(tool="next", condition=". > 5", expr="{value: .}")

    await callback_execution(10, callback)

    assert app.tools.detached_seen == [True]
    assert app.tools.offloads == [True]
    assert in_detached_run() is False


@pytest.mark.parametrize(("gate_enabled", "capable"), [(False, True), (True, False)])
async def test_callback_binds_the_worker_secret_capability(app, access_control, gate_enabled: bool, capable: bool):
    # A dequeued callback's follow-up tool sees the same worker-bound capability as
    # a dequeued task: OFF -> secret-capable, ON -> fail-closed, reset after.
    access_control(gate_enabled)
    app.tools.run_result = "chained-result"
    callback = CallbackSchema(tool="next", condition=". > 5", expr="{value: .}")

    await callback_execution(10, callback)

    assert app.tools.secret_capability_seen == [capable]
    assert caller_may_read_secrets() is False


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


async def test_prepare_backend_kwargs_injects_tool_name_and_stamps_capability():
    async def sample(x: int) -> int:
        return x

    # No request bound, so the stamped capability is the fail-closed default False;
    # the worker pops it before running the tool.
    kwargs = await prepare_backend_kwargs(sample, "backend_tool_name", "sample", {"x": 1})
    assert kwargs == {"x": 1, "backend_tool_name": "sample", "backend_secret_capability": False}


async def test_prepare_backend_kwargs_strips_fastmcp_context():
    async def sample(x: int, ctx: Context) -> int:
        return x

    kwargs = await prepare_backend_kwargs(sample, "backend_tool_name", "sample", {"x": 1, "ctx": object()})
    assert kwargs == {"x": 1, "backend_tool_name": "sample", "backend_secret_capability": False}


async def test_callback_schema_round_trips_through_validation():
    original = CallbackSchema(tool="t", condition=". > 1", expr="{a: .}")
    restored = CallbackSchema.model_validate(original.model_dump())
    assert restored == original


async def test_callback_jq_eval_is_timeout_bounded(app, monkeypatch):
    # The callback path evaluates jq through ``run_jq_first``, so a slow program
    # is aborted by JQ_TIMEOUT_SECONDS and the named TimeoutError is raised.
    class _SlowProgram:
        def input(self, payload):
            return self

        def first(self):
            time.sleep(1)
            return None

    monkeypatch.setattr(jq_util, "get_compiled_jq", lambda expr, prelude="": _SlowProgram())
    monkeypatch.setenv("JQ_TIMEOUT_SECONDS", "0.01")
    reset_all_settings()
    try:
        callback = CallbackSchema(tool="t", condition=". > 5")
        start = time.monotonic()
        with pytest.raises(TimeoutError, match="JQ_TIMEOUT_SECONDS"):
            await callback_execution(10, callback)
        assert time.monotonic() - start < 0.5
    finally:
        reset_all_settings()


def test_bare_condition_syntax_error_propagates():
    """A broken jq filter raises loudly instead of silently passing."""
    import asyncio

    callback = CallbackSchema(tool="t", condition="((broken")
    with pytest.raises(ValueError, match="syntax error"):
        asyncio.run(callback_execution(1, callback))
