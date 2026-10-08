"""Unit tests for the LangGraph run-config builders.

Exercises ``init_langgraph_config`` against the recording ``tai42_app`` bound in
``conftest.py``: thread-id defaulting, the :class:`~tai42_kit.llm.RunTrace` it returns,
the kit monitoring callback handler appended for the recording writer (with the
``create_agent`` grouping nodes and the reference chain payloads), the ``TraceContext``
it is bound to, and preservation of an existing config's ``configurable`` and
``callbacks``. No live monitoring backend is involved.

The builder touches NO OpenTelemetry context: a root run no longer attaches a fresh,
empty context, so a caller's ambient attribution survives onto the run's trace.

``build_run_config`` — the overlay every honoring agent builds its run config through — is
exercised here too: the memory keys and the step bound it overlays, the caller keys it
preserves, and the copy-by-value discipline that keeps a caller's config dict out of the
returned config.
"""

from __future__ import annotations

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from opentelemetry import context as otel_context
from tai42_contract.app import tai42_app
from tai42_contract.monitoring import MONITORING_PARENT_SPAN_ID_KEY, MONITORING_TRACE_ID_KEY, TraceContext
from tai42_kit.llm import MonitoringCallbackHandler, RunTrace
from tai42_kit.settings import reset_all_settings

from tai42_agents._internal.config_util import (
    CREATE_AGENT_GROUPING_NODES,
    build_run_config,
    init_langgraph_config,
    with_run_trace_lineage,
)
from tai42_agents.settings import AgentsLimitsSettings, agents_limits_settings


def _callbacks(trace: RunTrace) -> list[object]:
    return trace.config["callbacks"]


def _handler(trace: RunTrace) -> MonitoringCallbackHandler:
    (handler,) = [cb for cb in _callbacks(trace) if isinstance(cb, MonitoringCallbackHandler)]
    return handler


def _bound(trace: RunTrace) -> TraceContext:
    return _handler(trace).trace_context


def test_returns_a_run_trace() -> None:
    trace = init_langgraph_config()
    assert isinstance(trace, RunTrace)
    assert trace.context == _bound(trace)
    assert isinstance(trace.config["configurable"]["thread_id"], str)


def test_the_handler_declares_the_create_agent_grouping_nodes_and_references() -> None:
    handler = _handler(init_langgraph_config())
    assert handler.grouping_nodes == frozenset({"model", "tools"}) == CREATE_AGENT_GROUPING_NODES
    assert handler.chain_payloads == "references"
    assert handler.writer is tai42_app.monitoring.active.writer


def test_thread_id_defaulted_when_absent() -> None:
    thread_id = init_langgraph_config().config["configurable"]["thread_id"]
    assert isinstance(thread_id, str)
    assert thread_id


def test_callbacks_appended_for_the_recording_writer() -> None:
    trace = init_langgraph_config()

    # The kit's one monitoring callback handler is appended.
    callbacks = _callbacks(trace)
    assert len(callbacks) == 1
    assert isinstance(callbacks[0], MonitoringCallbackHandler)
    # It is bound to a fresh TraceContext built from the (auto-generated) trace id.
    ctx = _bound(trace)
    assert ctx.trace_id
    assert ctx.parent_span_id is None


def test_existing_config_is_preserved_and_extended() -> None:
    existing_cb = BaseCallbackHandler()
    existing = {
        "configurable": {
            "thread_id": "keep-me",
            MONITORING_TRACE_ID_KEY: "trace-123",
            MONITORING_PARENT_SPAN_ID_KEY: "parent-9",
        },
        "callbacks": [existing_cb],
    }

    result = init_langgraph_config(existing)

    # A new config is returned, not the caller's object.
    assert result.config is not existing
    # The caller's thread id is carried over.
    assert result.config["configurable"]["thread_id"] == "keep-me"
    # Existing callbacks kept; monitoring callbacks appended after them.
    callbacks = _callbacks(result)
    assert callbacks[0] is existing_cb
    assert len(callbacks) == 2
    # The explicit trace/parent ids flow into the TraceContext.
    ctx = _bound(result)
    assert ctx.trace_id == "trace-123"
    assert ctx.parent_span_id == "parent-9"
    assert result.context.trace_id == "trace-123"
    assert result.context.parent_span_id == "parent-9"


def test_ambient_trace_context_is_adopted_when_no_explicit_id() -> None:
    # A flow driving this agent as a node deposits its trace on the ambient carrier; with no
    # explicit `monitoring_trace_id`, the agent JOINS that trace (id + anchor) instead of
    # minting a fresh, orphaned one.
    from tai42_contract.monitoring import TraceContext, ambient_trace_context

    with ambient_trace_context(TraceContext(trace_id="flow-trace", parent_span_id="flow-span")):
        trace = init_langgraph_config()

    ctx = _bound(trace)
    assert ctx.trace_id == "flow-trace"
    assert ctx.parent_span_id == "flow-span"
    assert trace.context.trace_id == "flow-trace"


def test_explicit_trace_id_wins_over_ambient() -> None:
    # An explicitly propagated `monitoring_trace_id` is authoritative — the ambient deposit
    # never overrides it.
    from tai42_contract.monitoring import TraceContext, ambient_trace_context

    existing = {"configurable": {MONITORING_TRACE_ID_KEY: "explicit-trace"}}
    with ambient_trace_context(TraceContext(trace_id="flow-trace", parent_span_id="flow-span")):
        trace = init_langgraph_config(existing)

    ctx = _bound(trace)
    assert ctx.trace_id == "explicit-trace"
    assert ctx.parent_span_id is None


def test_fresh_trace_minted_when_no_explicit_and_no_ambient() -> None:
    # The standalone default: a fresh 32-char root trace id, no parent span, when neither an
    # explicit id nor an ambient deposit is present.
    from tai42_contract.monitoring import get_ambient_trace_context

    assert get_ambient_trace_context() is None
    ctx = _bound(init_langgraph_config())
    assert ctx.trace_id is not None
    assert len(ctx.trace_id) == 32
    assert "-" not in ctx.trace_id
    assert ctx.parent_span_id is None


@pytest.mark.parametrize(
    ("config", "scenario"),
    [
        (None, "fresh root (no explicit id, no ambient)"),
        (
            {"configurable": {MONITORING_TRACE_ID_KEY: "explicit-root"}},
            "explicit root (caller propagated id, no parent)",
        ),
    ],
)
def test_root_run_does_not_reset_the_otel_context(config: dict | None, scenario: str) -> None:
    # Both live-caller root doors reach the branch that USED to attach a fresh, empty OTel
    # context (``parent_span_id`` is None). The builder now touches no OTel context, so a
    # caller's ambient attribution (what the backend stamps via ``trace_attributes``)
    # survives the build and reaches the run's trace, instead of being wiped.
    key = otel_context.create_key("attribution-probe")
    token = otel_context.attach(otel_context.set_value(key, "user+session+tags"))
    try:
        init_langgraph_config(config)
        assert otel_context.get_value(key) == "user+session+tags", scenario
    finally:
        otel_context.detach(token)


def test_nested_run_keeps_the_otel_context() -> None:
    # A nested run (an explicit parent span pinned) likewise leaves the ambient OTel context
    # intact — no reset on any door.
    key = otel_context.create_key("attribution-probe-nested")
    token = otel_context.attach(otel_context.set_value(key, "live"))
    try:
        init_langgraph_config({"configurable": {MONITORING_TRACE_ID_KEY: "t", MONITORING_PARENT_SPAN_ID_KEY: "p"}})
        assert otel_context.get_value(key) == "live"
    finally:
        otel_context.detach(token)


def test_top_level_recursion_limit_is_preserved() -> None:
    # ``recursion_limit`` is a standard ``RunnableConfig`` top-level key; the
    # builder copies the incoming mapping by value, so an overlaid limit (a falsy
    # ``0`` too) survives onto the returned config the graph is invoked with. This
    # is the seam the tools/retrieval agents honor the parameter through.
    assert init_langgraph_config({"recursion_limit": 0}).config["recursion_limit"] == 0
    assert init_langgraph_config({"recursion_limit": 12}).config["recursion_limit"] == 12


def test_default_recursion_limit_setting_is_fifty() -> None:
    # The package's safe default step ceiling — positive, never unlimited.
    assert AgentsLimitsSettings().default_recursion_limit == 50


def test_default_recursion_limit_applied_when_run_pins_none() -> None:
    # A run that pins no ``recursion_limit`` gets the settings default onto the
    # effective config the graph is invoked with, so no run is uncapped.
    result = init_langgraph_config().config
    assert result["recursion_limit"] == agents_limits_settings().default_recursion_limit
    assert result["recursion_limit"] == 50


def test_caller_recursion_limit_wins_over_default() -> None:
    # A caller-supplied limit is left untouched; the default only fills a gap.
    assert init_langgraph_config(build_run_config(None, recursion_limit=7)).config["recursion_limit"] == 7
    # ``0`` is a real pinned value the caller chose, not an unset gap to fill.
    assert init_langgraph_config({"recursion_limit": 0}).config["recursion_limit"] == 0


def test_default_recursion_limit_env_override_flows_into_effective_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``TAI_AGENTS_DEFAULT_RECURSION_LIMIT`` overrides the fill-in default and the
    # override reaches the effective config the graph runs with.
    monkeypatch.setenv("TAI_AGENTS_DEFAULT_RECURSION_LIMIT", "3")
    reset_all_settings()
    try:
        assert init_langgraph_config().config["recursion_limit"] == 3
    finally:
        monkeypatch.delenv("TAI_AGENTS_DEFAULT_RECURSION_LIMIT", raising=False)
        reset_all_settings()


def test_input_config_is_not_mutated() -> None:
    existing = {
        "configurable": {MONITORING_TRACE_ID_KEY: "trace-abc"},
        "callbacks": ["existing-cb"],
    }

    init_langgraph_config(existing)

    # The caller's dict, its ``configurable`` section, and its ``callbacks``
    # list are all left exactly as they were passed in.
    assert existing == {
        "configurable": {MONITORING_TRACE_ID_KEY: "trace-abc"},
        "callbacks": ["existing-cb"],
    }


def test_same_input_yields_independent_thread_ids_and_callbacks() -> None:
    # A single config object fanned out to parallel voters must not let them
    # collide on a shared thread id or accumulate each other's callbacks.
    shared = {"configurable": {}, "callbacks": ["existing-cb"]}

    first = init_langgraph_config(shared).config
    second = init_langgraph_config(shared).config

    # Each call gets its own fresh, distinct thread id.
    assert first["configurable"]["thread_id"] != second["configurable"]["thread_id"]
    # Callbacks are not accumulated across calls: each result carries exactly
    # the caller's callbacks plus one set of monitoring callbacks.
    assert first["callbacks"][0] == "existing-cb"
    assert len(first["callbacks"]) == 2
    assert second["callbacks"][0] == "existing-cb"
    assert len(second["callbacks"]) == 2
    # The shared input is untouched by either call.
    assert shared == {"configurable": {}, "callbacks": ["existing-cb"]}


def test_build_run_config_overlays_memory_keys_and_recursion_limit() -> None:
    # The memory keys land in ``configurable`` (``resume_checkpoint_id`` under the
    # LangGraph name ``checkpoint_id``); ``recursion_limit`` overlays the top level.
    config = build_run_config(None, "t-1", "cp-9", 0)
    assert config["configurable"] == {"thread_id": "t-1", "checkpoint_id": "cp-9"}
    assert config["recursion_limit"] == 0


def test_build_run_config_explicit_keys_win_over_the_base() -> None:
    base = {"configurable": {"thread_id": "from-base", "checkpoint_id": "cp-base"}, "recursion_limit": 12}
    config = build_run_config(base, "t-1", "cp-9", 3)
    assert config["configurable"]["thread_id"] == "t-1"
    assert config["configurable"]["checkpoint_id"] == "cp-9"
    assert config["recursion_limit"] == 3


def test_build_run_config_preserves_the_base_and_never_aliases_it() -> None:
    base = {"configurable": {"tenant": "acme", "thread_id": "t-base"}, "tags": ["t1"], "metadata": {"origin": "api"}}

    config = build_run_config(base, None, None, None)

    # Every caller key survives — the base's own pinned thread included.
    assert config == base
    # ...on a fresh dict, so no run can scribble on a config shared with another.
    assert config is not base
    assert config["configurable"] is not base["configurable"]

    config["configurable"]["thread_id"] = "overwritten"
    assert base["configurable"]["thread_id"] == "t-base"


def test_with_run_trace_lineage_threads_a_root_context() -> None:
    from tai42_contract.monitoring import TraceContext

    base = {"configurable": {"thread_id": "keep"}}
    out = with_run_trace_lineage(base, TraceContext(trace_id="run-1"))
    # The shared trace id is threaded; a root context pins no parent anchor.
    assert out["configurable"][MONITORING_TRACE_ID_KEY] == "run-1"
    assert MONITORING_PARENT_SPAN_ID_KEY not in out["configurable"]
    # The caller's own keys survive and the input is left untouched.
    assert out["configurable"]["thread_id"] == "keep"
    assert base == {"configurable": {"thread_id": "keep"}}


def test_with_run_trace_lineage_threads_a_nested_anchor() -> None:
    from tai42_contract.monitoring import TraceContext

    out = with_run_trace_lineage(None, TraceContext(trace_id="run-1", parent_span_id="anchor"))
    # A context carrying an anchor threads the parent span id too, so the sub-run nests under it.
    assert out["configurable"][MONITORING_TRACE_ID_KEY] == "run-1"
    assert out["configurable"][MONITORING_PARENT_SPAN_ID_KEY] == "anchor"


def test_build_run_config_keyless_run_pins_no_thread() -> None:
    # With no memory key the ``configurable`` section carries no ``thread_id``, so
    # ``init_langgraph_config`` mints a fresh isolated one per run rather than the
    # runs colliding on one shared checkpoint thread.
    assert build_run_config(None) == {"configurable": {}}
    first = init_langgraph_config(build_run_config(None)).config["configurable"]["thread_id"]
    second = init_langgraph_config(build_run_config(None)).config["configurable"]["thread_id"]
    assert first != second
