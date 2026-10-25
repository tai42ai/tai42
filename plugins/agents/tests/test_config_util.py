"""Unit tests for the LangGraph run-config builders.

Exercises ``init_langgraph_config`` against the recording ``tai42_app`` bound in
``conftest.py``: thread-id defaulting, the kit monitoring callback handler appended only
for a run's lineage (with the ``create_agent`` grouping nodes and the reference chain
payloads, bound to that lineage), and preservation of an existing config's
``configurable`` and ``callbacks``. No live monitoring backend is involved.

The builder touches NO OpenTelemetry context, so a caller's ambient attribution survives
onto the run's trace.

``build_run_config`` — the overlay every honoring agent builds its run config through — is
exercised here too: the memory keys and the step bound it overlays, the caller keys it
preserves, and the copy-by-value discipline that keeps a caller's config dict out of the
returned config. ``with_run_trace_lineage`` writes a run's lineage into a sub-run's config.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from opentelemetry import context as otel_context
from tai42_contract.app import tai42_app
from tai42_contract.monitoring import MONITORING_PARENT_SPAN_ID_KEY, MONITORING_TRACE_ID_KEY, TraceContext
from tai42_kit.llm import MonitoringCallbackHandler
from tai42_kit.settings import reset_all_settings

from tai42_agents._internal.config_util import (
    CREATE_AGENT_GROUPING_NODES,
    build_run_config,
    init_langgraph_config,
    with_run_trace_lineage,
)
from tai42_agents.settings import AgentsLimitsSettings, agents_limits_settings

TRACE_A = "a" * 32
TRACE_E = "e" * 32
SPAN_A = "a" * 16
SPAN_B = "b" * 16
CONTEXT = TraceContext(trace_id=TRACE_A, parent_span_id=SPAN_A)


def _handler(config: dict[str, Any]) -> MonitoringCallbackHandler:
    (handler,) = [cb for cb in config["callbacks"] if isinstance(cb, MonitoringCallbackHandler)]
    return handler


def test_a_context_binds_one_handler_to_it() -> None:
    config = init_langgraph_config(None, CONTEXT)
    assert len(config["callbacks"]) == 1
    assert _handler(config).trace_context == CONTEXT
    assert isinstance(config["configurable"]["thread_id"], str)


def test_no_context_binds_no_handler() -> None:
    existing_cb = BaseCallbackHandler()
    config = init_langgraph_config({"callbacks": [existing_cb]}, None)
    assert config["callbacks"] == [existing_cb]


def test_the_handler_declares_the_create_agent_grouping_nodes_and_references() -> None:
    handler = _handler(init_langgraph_config(None, CONTEXT))
    assert handler.grouping_nodes == frozenset({"model", "tools"}) == CREATE_AGENT_GROUPING_NODES
    assert handler.chain_payloads == "references"
    assert handler.writer is tai42_app.monitoring.active.writer


def test_thread_id_defaulted_when_absent() -> None:
    thread_id = init_langgraph_config(None, None)["configurable"]["thread_id"]
    assert isinstance(thread_id, str)
    assert thread_id


def test_existing_config_is_preserved_and_extended() -> None:
    existing_cb = BaseCallbackHandler()
    existing = {
        "configurable": {
            "thread_id": "keep-me",
            MONITORING_TRACE_ID_KEY: TRACE_A,
            MONITORING_PARENT_SPAN_ID_KEY: SPAN_A,
        },
        "callbacks": [existing_cb],
    }

    result = init_langgraph_config(existing, CONTEXT)

    # A new config is returned, not the caller's object.
    assert result is not existing
    # The caller's configurable keys are carried over.
    assert result["configurable"] == existing["configurable"]
    # Existing callbacks kept; the monitoring handler appended after them.
    assert result["callbacks"][0] is existing_cb
    assert len(result["callbacks"]) == 2
    assert _handler(result).trace_context == CONTEXT


@pytest.mark.parametrize("context", [None, CONTEXT], ids=["no-lineage", "lineage"])
def test_the_builder_does_not_touch_the_otel_context(context: TraceContext | None) -> None:
    # A caller's ambient attribution (what the backend stamps via ``trace_attributes``)
    # survives the build and reaches the run's trace.
    key = otel_context.create_key("attribution-probe")
    token = otel_context.attach(otel_context.set_value(key, "user+session+tags"))
    try:
        init_langgraph_config(None, context)
        assert otel_context.get_value(key) == "user+session+tags"
    finally:
        otel_context.detach(token)


def test_top_level_recursion_limit_is_preserved() -> None:
    # ``recursion_limit`` is a standard ``RunnableConfig`` top-level key; the
    # builder copies the incoming mapping by value, so an overlaid limit (a falsy
    # ``0`` too) survives onto the returned config the graph is invoked with. This
    # is the seam the tools/retrieval agents honor the parameter through.
    assert init_langgraph_config({"recursion_limit": 0}, None)["recursion_limit"] == 0
    assert init_langgraph_config({"recursion_limit": 12}, None)["recursion_limit"] == 12


def test_default_recursion_limit_setting_is_fifty() -> None:
    # The package's safe default step ceiling — positive, never unlimited.
    assert AgentsLimitsSettings().default_recursion_limit == 50


def test_default_recursion_limit_applied_when_run_pins_none() -> None:
    # A run that pins no ``recursion_limit`` gets the settings default onto the
    # effective config the graph is invoked with, so no run is uncapped.
    result = init_langgraph_config(None, None)
    assert result["recursion_limit"] == agents_limits_settings().default_recursion_limit
    assert result["recursion_limit"] == 50


def test_caller_recursion_limit_wins_over_default() -> None:
    # A caller-supplied limit is left untouched; the default only fills a gap.
    assert init_langgraph_config(build_run_config(None, recursion_limit=7), None)["recursion_limit"] == 7
    # ``0`` is a real pinned value the caller chose, not an unset gap to fill.
    assert init_langgraph_config({"recursion_limit": 0}, None)["recursion_limit"] == 0


def test_default_recursion_limit_env_override_flows_into_effective_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``TAI_AGENTS_DEFAULT_RECURSION_LIMIT`` overrides the fill-in default and the
    # override reaches the effective config the graph runs with.
    monkeypatch.setenv("TAI_AGENTS_DEFAULT_RECURSION_LIMIT", "3")
    reset_all_settings()
    try:
        assert init_langgraph_config(None, None)["recursion_limit"] == 3
    finally:
        monkeypatch.delenv("TAI_AGENTS_DEFAULT_RECURSION_LIMIT", raising=False)
        reset_all_settings()


def test_input_config_is_not_mutated() -> None:
    existing = {
        "configurable": {MONITORING_TRACE_ID_KEY: TRACE_A, MONITORING_PARENT_SPAN_ID_KEY: SPAN_A},
        "callbacks": ["existing-cb"],
    }

    init_langgraph_config(existing, CONTEXT)

    # The caller's dict, its ``configurable`` section, and its ``callbacks``
    # list are all left exactly as they were passed in.
    assert existing == {
        "configurable": {MONITORING_TRACE_ID_KEY: TRACE_A, MONITORING_PARENT_SPAN_ID_KEY: SPAN_A},
        "callbacks": ["existing-cb"],
    }


def test_same_input_yields_independent_thread_ids_and_callbacks() -> None:
    # A single config object fanned out to parallel voters must not let them
    # collide on a shared thread id or accumulate each other's callbacks.
    shared = {"configurable": {}, "callbacks": ["existing-cb"]}

    first = init_langgraph_config(shared, CONTEXT)
    second = init_langgraph_config(shared, CONTEXT)

    # Each call gets its own fresh, distinct thread id.
    assert first["configurable"]["thread_id"] != second["configurable"]["thread_id"]
    # Callbacks are not accumulated across calls: each result carries exactly
    # the caller's callbacks plus one monitoring handler.
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


def test_with_run_trace_lineage_threads_the_lineage() -> None:
    base = {"configurable": {"thread_id": "keep"}}
    out = with_run_trace_lineage(base, TraceContext(trace_id=TRACE_E, parent_span_id=SPAN_B))
    # Both ids are threaded, so the sub-run nests under the lineage's span.
    assert out["configurable"][MONITORING_TRACE_ID_KEY] == TRACE_E
    assert out["configurable"][MONITORING_PARENT_SPAN_ID_KEY] == SPAN_B
    # The caller's own keys survive and the input is left untouched.
    assert out["configurable"]["thread_id"] == "keep"
    assert base == {"configurable": {"thread_id": "keep"}}


def test_with_run_trace_lineage_writes_no_key_without_a_lineage() -> None:
    base = {"configurable": {"thread_id": "keep"}}
    out = with_run_trace_lineage(base, None)
    assert out == base
    assert out is not base
    assert out["configurable"] is not base["configurable"]


def test_build_run_config_keyless_run_pins_no_thread() -> None:
    # With no memory key the ``configurable`` section carries no ``thread_id``, so
    # ``init_langgraph_config`` mints a fresh isolated one per run rather than the
    # runs colliding on one shared checkpoint thread.
    assert build_run_config(None) == {"configurable": {}}
    first = init_langgraph_config(build_run_config(None), None)["configurable"]["thread_id"]
    second = init_langgraph_config(build_run_config(None), None)["configurable"]["thread_id"]
    assert first != second
