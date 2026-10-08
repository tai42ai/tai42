"""Build the LangGraph run config for an agent invocation.

* :func:`build_run_config` overlays a run's memory keys (``thread_id``,
  ``resume_checkpoint_id``) and ``recursion_limit`` onto the caller's
  ``langgraph_config`` base.
* :func:`init_langgraph_config` ensures a ``thread_id`` and a ``recursion_limit``,
  then delegates to the kit's :func:`~tai42_kit.llm.bind_run_trace` to resolve the
  run's trace lineage and append the kit's monitoring callbacks (declaring the
  ``create_agent`` graph's grouping nodes), returning the
  :class:`~tai42_kit.llm.RunTrace` whose ``config`` the graph is invoked with.
* :func:`with_run_trace_lineage` threads a resolved trace context onto a base config so
  a FEATURE run that drives several sub-runs (an evaluator/critic loop, parallel voters
  plus a judge) binds ONE lineage at its entry and nests every sub-run under it.

Both builders treat the caller's config as read-only, copying the mapping, its
``configurable``, and its ``callbacks`` by value so one base config can feed many
parallel invocations without colliding on ``thread_id`` or accumulating each other's
callbacks.
"""

from __future__ import annotations

import uuid
from typing import Any

from tai42_contract.monitoring import MONITORING_PARENT_SPAN_ID_KEY, MONITORING_TRACE_ID_KEY, TraceContext
from tai42_kit.llm import RunTrace, bind_run_trace

from tai42_agents.settings import agents_limits_settings

# The nodes of LangChain's ``create_agent`` graph (also under deepagents) that hold the
# agent's steps rather than being one: the model call node and the tool-calls node.
CREATE_AGENT_GROUPING_NODES: frozenset[str] = frozenset({"model", "tools"})


def build_run_config(
    langgraph_config: dict[str, Any] | None,
    thread_id: str | None = None,
    resume_checkpoint_id: str | None = None,
    recursion_limit: int | None = None,
) -> dict[str, Any]:
    """Overlay a run's memory keys and step bound onto the caller's config base.

    ``thread_id`` and ``resume_checkpoint_id`` (mapped to ``checkpoint_id``) overlay
    the ``configurable`` section, winning over the same keys in the base;
    ``recursion_limit`` overlays the top level. The base is read-only — the written
    sections are rebuilt, never mutated — so two runs sharing a base cannot scribble
    into each other's config. With no memory key set, ``configurable`` passes
    through as the base carries it; :func:`init_langgraph_config` mints a fresh
    ``thread_id`` for a keyless run.
    """
    config = dict(langgraph_config or {})
    configurable = dict(config.get("configurable", {}))
    if thread_id is not None:
        configurable["thread_id"] = thread_id
    if resume_checkpoint_id is not None:
        configurable["checkpoint_id"] = resume_checkpoint_id
    config["configurable"] = configurable
    if recursion_limit is not None:
        config["recursion_limit"] = recursion_limit
    return config


def init_langgraph_config(config: dict[str, Any] | None = None) -> RunTrace:
    """Ensure a ``thread_id`` + ``recursion_limit``, then bind the run's trace and callbacks.

    Overlays the langgraph-shaped concerns this package owns — a fresh ``thread_id`` for
    a keyless run, the settings-default ``recursion_limit`` (a positive ceiling, so no
    top-level graph runs uncapped; a caller-supplied value, ``0`` included, wins) — onto
    a by-value copy of the caller's config, then returns
    :func:`~tai42_kit.llm.bind_run_trace`'s :class:`~tai42_kit.llm.RunTrace`. The kit
    resolves the trace lineage and appends its monitoring callbacks, which mark the
    ``create_agent`` graph's :data:`CREATE_AGENT_GROUPING_NODES` as grouping steps and
    record every chain value once, referenced from later records; the caller runs the
    graph with the returned ``RunTrace.config``.

    The ``recursion_limit`` bounds the top-level graph only — each deep-agent task-tool
    subagent runs its own graph under deepagents' own bound.
    """
    source = config or {}
    new_config = dict(source)
    configurable = dict(source.get("configurable", {}))
    new_config["configurable"] = configurable

    if "thread_id" not in configurable:
        configurable["thread_id"] = str(uuid.uuid4())

    if "recursion_limit" not in new_config:
        new_config["recursion_limit"] = agents_limits_settings().default_recursion_limit

    return bind_run_trace(new_config, grouping_nodes=CREATE_AGENT_GROUPING_NODES)


def with_run_trace_lineage(config: dict[str, Any] | None, context: TraceContext) -> dict[str, Any]:
    """Thread a resolved trace ``context`` onto a by-value copy of ``config``'s ``configurable``.

    Sets :data:`~tai42_contract.monitoring.MONITORING_TRACE_ID_KEY` (and
    :data:`~tai42_contract.monitoring.MONITORING_PARENT_SPAN_ID_KEY` when the context
    carries an anchor) so the sub-run this config drives JOINS ``context``'s trace rather
    than minting its own root. A FEATURE run that drives several sub-runs resolves ONE
    lineage at its entry (:func:`~tai42_kit.llm.resolve_trace_context`) and derives each
    sub-run's config through here, so every sub-run nests under the one run trace. The
    caller's mapping and its ``configurable`` are left untouched.
    """
    new_config = dict(config or {})
    configurable = dict(new_config.get("configurable", {}))
    configurable[MONITORING_TRACE_ID_KEY] = context.trace_id
    if context.parent_span_id is not None:
        configurable[MONITORING_PARENT_SPAN_ID_KEY] = context.parent_span_id
    new_config["configurable"] = configurable
    return new_config
