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
from tai42_contract.access_control import caller_may_read_secrets
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText

from tai42_kit.backend import CallbackSchema, callback_execution, carry_forwarded_fire, prepare_backend_kwargs
from tai42_kit.registry import StagedSlot
from tai42_kit.settings.cache_registry import reset_all_settings
from tai42_kit.utils import worker_secret_capability as capability_module
from tai42_kit.utils.data import jq_util
from tai42_kit.utils.detached_util import in_detached_run
from tai42_kit.utils.schedule_subject import (
    SCHEDULE_EXECUTION_FINGERPRINT_ARG,
    SCHEDULE_EXECUTION_KEY_ARG,
    SCHEDULE_SUBJECT_ARG,
)
from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG, set_access_control_gate_state

_FORWARDED = {
    SCHEDULE_SUBJECT_ARG: {"target_kind": "app", "target_name": "svc", "kind": "user", "key": "u1"},
    SCHEDULE_EXECUTION_KEY_ARG: "svc",
    SCHEDULE_EXECUTION_FINGERPRINT_ARG: "fp-1",
}


class _FakeResourceManager:
    """Returns inline content unchanged and resolves a stored id from a map."""

    def __init__(self) -> None:
        self.templates: dict[str, str] = {}

    async def render_templated_text(self, text: TemplatedText, locale: str | None = None) -> str:
        if text.id is not None:
            return self.templates[text.id]
        assert text.content is not None
        return text.content


class _FakeTools:
    """Records each ``run_tool`` call, the detached-run flag and secret-read capability it
    observed, and the offload flag it was dispatched with."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.detached_seen: list[bool] = []
        self.capability_seen: list[bool] = []
        self.offloads: list[bool] = []
        self.result: Any = "ran"

    async def run_tool(self, key: str, arguments: Any, *, offload_sync: bool = False) -> Any:
        self.calls.append((key, arguments))
        self.detached_seen.append(in_detached_run())
        self.capability_seen.append(caller_may_read_secrets())
        self.offloads.append(offload_sync)
        return self.result


class _FakeApp:
    def __init__(self) -> None:
        self.storage = SimpleNamespace(resource_manager=_FakeResourceManager())
        self.tools = _FakeTools()


@pytest.fixture(autouse=True)
def _undeclared_gate_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capability_module, "_GATE_STATE", StagedSlot())


def _carrying(capability: bool = False, **fields: Any) -> CallbackSchema:
    """A callback spec as the enqueue path leaves it: carrying the job's secret-read capability."""
    return CallbackSchema(carried_kwargs={WORKER_SECRET_CAPABILITY_ARG: capability}, **fields)


@pytest.fixture
def bound_app():
    fake = _FakeApp()
    with tai42_app.bound(fake):
        yield fake


# -- prepare_backend_kwargs -------------------------------------------------


async def test_prepare_backend_kwargs_injects_tool_name() -> None:
    async def some_tool(a: int, b: str = "x") -> None: ...

    kwargs = await prepare_backend_kwargs(some_tool, "backend_tool_name", "some_tool", {"a": 1})
    # No request is bound in the test, so the stamped capability is the fail-closed
    # default False; the submit seam always stamps it so the worker binds it verbatim.
    assert kwargs == {"a": 1, "backend_tool_name": "some_tool", "backend_secret_capability": False}


async def test_prepare_backend_kwargs_strips_fastmcp_context() -> None:
    async def some_tool(x: int, ctx: Context) -> int:
        return x

    kwargs = await prepare_backend_kwargs(some_tool, "backend_tool_name", "some_tool", {"x": 1, "ctx": object()})
    assert kwargs == {"x": 1, "backend_tool_name": "some_tool", "backend_secret_capability": False}


# -- render methods ---------------------------------------------------------


async def test_rendered_fields_resolve_through_resource_manager(bound_app) -> None:
    inline = CallbackSchema(condition=TemplatedText(content=".ok"), expr=TemplatedText(content=".x"))
    assert await inline.rendered_condition() == ".ok"
    assert await inline.rendered_expr() == ".x"

    bound_app.storage.resource_manager.templates["cond-1"] = ". != null"
    by_id = CallbackSchema(condition=TemplatedText(id="cond-1"))
    assert await by_id.rendered_condition() == ". != null"

    empty = CallbackSchema()
    assert await empty.rendered_condition() == ""
    assert await empty.rendered_expr() == ""


# -- callback_execution -----------------------------------------------------


async def test_condition_pass_runs_tool_with_transformed_value(bound_app) -> None:
    callback = _carrying(condition=TemplatedText(content=".ok"), expr=TemplatedText(content="{x: .value}"), tool="next")
    out = await callback_execution({"ok": True, "value": 5}, callback)
    assert out == "ran"
    assert bound_app.tools.calls == [("next", {"x": 5})]


async def test_condition_fail_returns_none(bound_app) -> None:
    callback = _carrying(condition=TemplatedText(content=".ok"), expr=TemplatedText(content="{x: .value}"), tool="next")
    out = await callback_execution({"ok": False, "value": 5}, callback)
    assert out is None
    assert bound_app.tools.calls == []


async def test_condition_empty_pipeline_skips(bound_app) -> None:
    # A condition that evaluates to an EMPTY pipeline (emits nothing) skips the
    # callback (returns None) rather than crashing with an opaque RuntimeError.
    callback = _carrying(
        condition=TemplatedText(content=".errors[] | select(.fatal)"),
        expr=TemplatedText(content="{x: .value}"),
        tool="next",
    )
    out = await callback_execution({"errors": [{"fatal": False}], "value": 5}, callback)
    assert out is None
    assert bound_app.tools.calls == []


async def test_expr_empty_pipeline_yields_empty_mapping(bound_app) -> None:
    # An expr that evaluates to an EMPTY pipeline yields {} (default), passed to
    # the tool as {} — never the opaque RuntimeError.
    callback = _carrying(
        condition=TemplatedText(content=".ok"), expr=TemplatedText(content=".errors[] | select(.fatal)"), tool="next"
    )
    out = await callback_execution({"ok": True, "errors": [{"fatal": False}]}, callback)
    assert out == "ran"
    assert bound_app.tools.calls == [("next", {})]


async def test_without_tool_returns_expr_output(bound_app) -> None:
    callback = CallbackSchema(expr=TemplatedText(content="{doubled: (.value * 2)}"))
    out = await callback_execution({"value": 4}, callback)
    assert out == {"doubled": 8}
    assert bound_app.tools.calls == []


async def test_without_expr_runs_tool_with_empty_args(bound_app) -> None:
    callback = _carrying(tool="next")
    out = await callback_execution({"value": 4}, callback)
    assert out == "ran"
    assert bound_app.tools.calls == [("next", {})]


async def test_callback_runs_tool_detached(bound_app) -> None:
    # A worker executes a dequeued callback with no live caller, so the follow-up
    # tool observes the detached flag set; the flag never leaks past the callback.
    callback = _carrying(condition=TemplatedText(content=".ok"), expr=TemplatedText(content="{x: .value}"), tool="next")
    await callback_execution({"ok": True, "value": 5}, callback)
    assert bound_app.tools.detached_seen == [True]
    # A dequeued callback offloads a blocking sync tool off the worker's event loop.
    assert bound_app.tools.offloads == [True]
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

    monkeypatch.setattr(jq_util, "get_compiled_jq", lambda expr, prelude="", variables=(): _SlowProgram())
    monkeypatch.setenv("JQ_TIMEOUT_SECONDS", "0.01")
    reset_all_settings()
    try:
        callback = _carrying(
            condition=TemplatedText(content=".ok"), expr=TemplatedText(content="{x: .value}"), tool="next"
        )
        start = time.monotonic()
        with pytest.raises(TimeoutError, match="JQ_TIMEOUT_SECONDS"):
            await callback_execution({"ok": True, "value": 5}, callback)
        assert time.monotonic() - start < 0.5
    finally:
        reset_all_settings()


# -- carry_forwarded_fire: the gate state and the forwarded pair onto the callback spec --------------


@pytest.mark.parametrize(("gate_enabled", "capability"), [(True, False), (False, True)])
def test_carry_forwarded_fire_always_carries_the_gate_state(gate_enabled: bool, capability: bool) -> None:
    # A callback runs a follow-up no caller re-authorizes, so it carries the access-control gate state
    # decided in the submitting process: gate ON fail-closes, gate OFF is the synthetic admin.
    set_access_control_gate_state(gate_enabled)
    callback = CallbackSchema(tool="next")
    carry_forwarded_fire(callback, {"text": "hi"})
    assert callback.carried_kwargs == {WORKER_SECRET_CAPABILITY_ARG: capability}


def test_carry_forwarded_fire_ignores_the_submitter_s_capability() -> None:
    # The task job's own stamp (the submitter's capability) is not what the callback carries.
    set_access_control_gate_state(True)
    callback = CallbackSchema(tool="next")
    carry_forwarded_fire(callback, {WORKER_SECRET_CAPABILITY_ARG: True})
    assert callback.carried_kwargs == {WORKER_SECRET_CAPABILITY_ARG: False}


def test_carry_forwarded_fire_stamps_the_pair_on_a_schema() -> None:
    # A task job that forwards a door subject/identity pair carries it onto the callback schema so the
    # follow-up job re-establishes the same door context.
    set_access_control_gate_state(True)
    callback = CallbackSchema(tool="next")
    carry_forwarded_fire(callback, dict(_FORWARDED))
    assert callback.carried_kwargs == {**_FORWARDED, WORKER_SECRET_CAPABILITY_ARG: False}


def test_carry_forwarded_fire_stamps_the_pair_on_a_raw_mapping() -> None:
    # Before it crosses the queue the spec is a plain mapping; the same keys are carried.
    set_access_control_gate_state(False)
    callback: dict[str, Any] = {"tool": "next"}
    carry_forwarded_fire(callback, dict(_FORWARDED))
    assert callback["carried_kwargs"] == {**_FORWARDED, WORKER_SECRET_CAPABILITY_ARG: True}


def test_carry_forwarded_fire_without_a_declared_gate_state_raises() -> None:
    with pytest.raises(RuntimeError, match="gate state was never declared"):
        carry_forwarded_fire(CallbackSchema(tool="next"), {})


# -- callback_execution binds the carried capability -------------------------------------------


@pytest.mark.parametrize("capability", [True, False])
async def test_callback_binds_the_carried_capability(bound_app, capability: bool) -> None:
    callback = _carrying(capability, tool="next")
    await callback_execution({"value": 1}, callback)
    assert bound_app.tools.capability_seen == [capability]
    assert caller_may_read_secrets() is False


async def test_callback_without_a_carried_capability_raises(bound_app) -> None:
    callback = CallbackSchema(tool="next")
    with pytest.raises(KeyError, match=WORKER_SECRET_CAPABILITY_ARG):
        await callback_execution({"value": 1}, callback)
    assert bound_app.tools.calls == []


@pytest.mark.parametrize("capability", [True, False])
async def test_a_re_run_of_the_same_callback_spec_binds_the_carried_capability_again(
    bound_app, capability: bool
) -> None:
    """A backend that retries a callback re-runs the spec object it was handed; the run leaves it intact."""
    callback = _carrying(capability, tool="next")
    before = dict(callback.carried_kwargs)
    await callback_execution({"value": 1}, callback)
    assert callback.carried_kwargs == before
    await callback_execution({"value": 1}, callback)
    assert bound_app.tools.capability_seen == [capability, capability]
