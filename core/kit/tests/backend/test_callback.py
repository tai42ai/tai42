"""The shared backend callback glue: rendering, condition gating, expression
transform, the detached-run bracket, and kwarg preparation.

``callback_execution`` evaluates real jq through ``run_jq_first``, so the
empty-pipeline semantics (a condition emitting nothing skips; an expression
emitting nothing yields ``{}``) are exercised end to end against a fake app the
``tai42_app`` handle binds to for the test.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Context
from tai42_contract.app import tai42_app

from tai42_kit.backend import CallbackSchema, callback_execution, prepare_backend_kwargs
from tai42_kit.settings.cache_registry import reset_all_settings
from tai42_kit.utils.data import jq_util
from tai42_kit.utils.detached_util import in_detached_run


class _FakeResourceManager:
    """Renders inline content unchanged and resolves a template id from a map."""

    def __init__(self) -> None:
        self.templates: dict[str, str] = {}

    async def render_by_id_or_content(self, content=None, template_id=None, kwargs=None) -> str:
        if template_id is not None:
            return self.templates[template_id]
        return content if content is not None else ""


class _FakeTools:
    """Records each ``run_tool`` call and the detached-run flag it observed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.detached_seen: list[bool] = []
        self.result: Any = "ran"

    async def run_tool(self, key: str, arguments: Any) -> Any:
        self.calls.append((key, arguments))
        self.detached_seen.append(in_detached_run())
        return self.result


class _FakeApp:
    def __init__(self) -> None:
        self.storage = SimpleNamespace(resource_manager=_FakeResourceManager())
        self.tools = _FakeTools()


@pytest.fixture
def bound_app():
    fake = _FakeApp()
    with tai42_app.bound(fake):
        yield fake


# -- prepare_backend_kwargs -------------------------------------------------


async def test_prepare_backend_kwargs_injects_tool_name() -> None:
    async def some_tool(a: int, b: str = "x") -> None: ...

    kwargs = await prepare_backend_kwargs(some_tool, "backend_tool_name", "some_tool", {"a": 1})
    assert kwargs == {"a": 1, "backend_tool_name": "some_tool"}


async def test_prepare_backend_kwargs_strips_fastmcp_context() -> None:
    async def some_tool(x: int, ctx: Context) -> int:
        return x

    kwargs = await prepare_backend_kwargs(some_tool, "backend_tool_name", "some_tool", {"x": 1, "ctx": object()})
    assert kwargs == {"x": 1, "backend_tool_name": "some_tool"}


# -- render methods ---------------------------------------------------------


async def test_rendered_fields_resolve_through_resource_manager(bound_app) -> None:
    inline = CallbackSchema(condition=".ok", expr=".x")
    assert await inline.rendered_condition() == ".ok"
    assert await inline.rendered_expr() == ".x"

    bound_app.storage.resource_manager.templates["cond-1"] = ". != null"
    by_id = CallbackSchema(condition_id="cond-1")
    assert await by_id.rendered_condition() == ". != null"

    empty = CallbackSchema()
    assert await empty.rendered_condition() == ""
    assert await empty.rendered_expr() == ""


# -- callback_execution -----------------------------------------------------


async def test_condition_pass_runs_tool_with_transformed_value(bound_app) -> None:
    callback = CallbackSchema(condition=".ok", expr="{x: .value}", tool="next")
    out = await callback_execution({"ok": True, "value": 5}, callback)
    assert out == "ran"
    assert bound_app.tools.calls == [("next", {"x": 5})]


async def test_condition_fail_returns_none(bound_app) -> None:
    callback = CallbackSchema(condition=".ok", expr="{x: .value}", tool="next")
    out = await callback_execution({"ok": False, "value": 5}, callback)
    assert out is None
    assert bound_app.tools.calls == []


async def test_condition_empty_pipeline_skips(bound_app) -> None:
    # A condition that evaluates to an EMPTY pipeline (emits nothing) skips the
    # callback (returns None) rather than crashing with an opaque RuntimeError.
    callback = CallbackSchema(condition=".errors[] | select(.fatal)", expr="{x: .value}", tool="next")
    out = await callback_execution({"errors": [{"fatal": False}], "value": 5}, callback)
    assert out is None
    assert bound_app.tools.calls == []


async def test_expr_empty_pipeline_yields_empty_mapping(bound_app) -> None:
    # An expr that evaluates to an EMPTY pipeline yields {} (default), passed to
    # the tool as {} — never the opaque RuntimeError.
    callback = CallbackSchema(condition=".ok", expr=".errors[] | select(.fatal)", tool="next")
    out = await callback_execution({"ok": True, "errors": [{"fatal": False}]}, callback)
    assert out == "ran"
    assert bound_app.tools.calls == [("next", {})]


async def test_without_tool_returns_expr_output(bound_app) -> None:
    callback = CallbackSchema(expr="{doubled: (.value * 2)}")
    out = await callback_execution({"value": 4}, callback)
    assert out == {"doubled": 8}
    assert bound_app.tools.calls == []


async def test_without_expr_runs_tool_with_empty_args(bound_app) -> None:
    callback = CallbackSchema(tool="next")
    out = await callback_execution({"value": 4}, callback)
    assert out == "ran"
    assert bound_app.tools.calls == [("next", {})]


async def test_callback_runs_tool_detached(bound_app) -> None:
    # A worker executes a dequeued callback with no live caller, so the follow-up
    # tool observes the detached flag set; the flag never leaks past the callback.
    callback = CallbackSchema(condition=".ok", expr="{x: .value}", tool="next")
    await callback_execution({"ok": True, "value": 5}, callback)
    assert bound_app.tools.detached_seen == [True]
    assert in_detached_run() is False


async def test_callback_jq_eval_is_timeout_bounded(bound_app, monkeypatch) -> None:
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
        callback = CallbackSchema(condition=".ok", expr="{x: .value}", tool="next")
        start = time.monotonic()
        with pytest.raises(TimeoutError, match="JQ_TIMEOUT_SECONDS"):
            await callback_execution({"ok": True, "value": 5}, callback)
        assert time.monotonic() - start < 0.5
    finally:
        reset_all_settings()
