"""Build the LangGraph run config for an agent invocation.

* :func:`build_run_config` overlays a run's memory keys (``thread_id``,
  ``resume_checkpoint_id``) and ``recursion_limit`` onto the caller's
  ``langgraph_config`` base.
* :func:`init_langgraph_config` ensures a ``thread_id`` and a ``recursion_limit``,
  then binds the kit's monitoring callbacks for the run's trace lineage through
  :func:`~tai42_kit.llm.bind_run_trace` (declaring the ``create_agent`` graph's grouping
  nodes), returning the config the graph is invoked with. The lineage is the one the
  drive's run-trace opener (:func:`~tai42_kit.monitoring.open_run_trace` or
  :func:`~tai42_kit.monitoring.begin_run_trace`) gave it.
* :func:`with_run_trace_lineage` writes a run's lineage into a sub-run's base config so
  a run that drives several sub-runs (an evaluator/critic loop, parallel voters plus a
  judge) nests every sub-run under its one lineage.
* :func:`config_addressed` reads the thread and checkpoint ids a ``langgraph_config``-shaped
  dict addresses, the answer every kind's ``addressed_memory`` builds from.
* :func:`minted_thread_id` / :func:`unmark_caller_thread` / :func:`mark_minted_thread_finished`
  keep the checkpoint finished-thread ledger: a caller-supplied thread leaves the ledger when
  a run starts on it; a thread the run minted is marked finished when the run ends without
  a park.

Both builders treat the caller's config as read-only, copying the mapping, its
``configurable``, and its ``callbacks`` by value so one base config can feed many
parallel invocations without colliding on ``thread_id`` or accumulating each other's
callbacks.
"""

from __future__ import annotations

import uuid
from typing import Any

from tai42_contract.monitoring import MONITORING_PARENT_SPAN_ID_KEY, MONITORING_TRACE_ID_KEY, TraceContext
from tai42_kit.llm import bind_run_trace
from tai42_kit.llm.checkpoint import mark_threads_active, mark_threads_finished

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


def thread_head_config(config: dict[str, Any]) -> dict[str, Any]:
    """``config`` addressing its thread's latest checkpoint: any pinned ``checkpoint_id`` is dropped.

    A drive started from a pinned checkpoint forks the thread and writes its own checkpoints after
    it, so what the drive ended at, and what a later resume continues from, is the thread's latest
    checkpoint, never the pin. The given config is not mutated.
    """
    configurable = {key: value for key, value in config.get("configurable", {}).items() if key != "checkpoint_id"}
    return {**config, "configurable": configurable}


def config_addressed(value: Any) -> tuple[set[str], set[str]]:
    """The ``(thread_ids, checkpoint_ids)`` a ``langgraph_config``-shaped value carries in ``configurable``.

    Anything that is not such a mapping addresses nothing.
    """
    if not isinstance(value, dict):
        return set(), set()
    configurable = value.get("configurable")
    if not isinstance(configurable, dict):
        return set(), set()
    thread_id = configurable.get("thread_id")
    checkpoint_id = configurable.get("checkpoint_id")
    threads = {str(thread_id)} if thread_id is not None else set()
    checkpoints = {str(checkpoint_id)} if checkpoint_id is not None else set()
    return threads, checkpoints


def init_langgraph_config(config: dict[str, Any] | None, context: TraceContext | None) -> dict[str, Any]:
    """Ensure a ``thread_id`` + ``recursion_limit``, then bind the monitoring callbacks for ``context``.

    Overlays the langgraph-shaped concerns this package owns — a fresh ``thread_id`` for
    a keyless run, the settings-default ``recursion_limit`` (a positive ceiling, so no
    top-level graph runs uncapped; a caller-supplied value, ``0`` included, wins) — onto
    a by-value copy of the caller's config, then returns
    :func:`~tai42_kit.llm.bind_run_trace`'s bound config. ``context`` is the run's lineage
    from its run-trace opener; with a context and a recording writer the kit appends its
    monitoring callbacks, which record the graph's top run under ``context``, mark the
    ``create_agent`` graph's :data:`CREATE_AGENT_GROUPING_NODES` as grouping steps and
    record every chain value once, referenced from later records. With ``None`` no
    callback is appended.

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

    return bind_run_trace(new_config, context, grouping_nodes=CREATE_AGENT_GROUPING_NODES)


def minted_thread_id(source: dict[str, Any] | None, run_config: dict[str, Any]) -> str | None:
    """The thread id :func:`init_langgraph_config` minted for this run, or ``None`` when ``source`` carried one."""
    if "thread_id" in ((source or {}).get("configurable") or {}):
        return None
    return run_config["configurable"]["thread_id"]


async def unmark_caller_thread(thread_id: str, *, provider: str | None) -> None:
    """Take a caller-supplied thread out of the finished-thread ledger before a run starts on it.

    ``provider`` is the run's checkpoint provider (``None`` = the deployment's), so the ledger is
    the one of the store that holds the thread.
    """
    await mark_threads_active([thread_id], provider=provider)


async def mark_minted_thread_finished(thread_id: str | None, *, provider: str | None) -> None:
    """Mark the thread this run minted finished, in its checkpoint provider's ledger (no-op for ``None``).

    The run owns a minted thread: nobody else knows its id, so its end is the thread's end. A
    caller-supplied thread is the caller's and is never marked here.
    """
    if thread_id is not None:
        await mark_threads_finished([thread_id], provider=provider)


async def start_run_thread(
    source: dict[str, Any] | None, run_config: dict[str, Any], *, provider: str | None
) -> str | None:
    """Ready the run's thread in the finished-thread ledger; return the minted thread id, if any.

    A caller-supplied thread is unmarked (a thread used again never sits in the ledger while live);
    a minted one is new and needs nothing.
    """
    minted = minted_thread_id(source, run_config)
    if minted is None:
        await unmark_caller_thread(run_config["configurable"]["thread_id"], provider=provider)
    return minted


def with_run_trace_lineage(config: dict[str, Any] | None, context: TraceContext | None) -> dict[str, Any]:
    """A by-value copy of ``config`` whose ``configurable`` names ``context``'s lineage.

    For a context, sets :data:`~tai42_contract.monitoring.MONITORING_TRACE_ID_KEY` and
    :data:`~tai42_contract.monitoring.MONITORING_PARENT_SPAN_ID_KEY`, so a sub-run driven with
    the config keeps that lineage instead of opening its own ``run`` span. A run that drives
    several sub-runs takes its one lineage from :func:`~tai42_kit.monitoring.begin_run_trace`
    or :func:`~tai42_kit.monitoring.open_run_trace` and derives each sub-run's config through
    here, so every sub-run nests under it. For ``None`` (nothing records), or a context naming no
    trace, no key is written.
    The caller's mapping and its ``configurable`` are left untouched.
    """
    new_config = dict(config or {})
    configurable = dict(new_config.get("configurable", {}))
    if context is not None and context.trace_id:
        configurable[MONITORING_TRACE_ID_KEY] = context.trace_id
        configurable[MONITORING_PARENT_SPAN_ID_KEY] = context.parent_span_id
    new_config["configurable"] = configurable
    return new_config
