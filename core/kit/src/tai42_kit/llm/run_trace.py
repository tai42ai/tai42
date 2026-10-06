"""The per-invoke run-trace seam: resolve a run's trace lineage and bind its callbacks.

Every FEATURE's model call carries monitoring through ONE building block here, so the
active backend records the call under the run's trace no matter which agent drove it.

* :func:`resolve_trace_context` resolves the ONE lineage a run traces under — an
  EXPLICITLY propagated ``monitoring_trace_id`` (a caller pinned it, e.g. a monitored
  driver invoking the run directly) wins; else the AMBIENT context a driver deposited
  when it drives this run as a node, so the run JOINS that trace instead of orphaning
  into a fresh one; else a freshly minted 32-hex root (the standalone default).
* :func:`bind_run_trace` resolves that lineage, asks the active monitoring backend for
  the run's callbacks under it, and returns the per-invoke ``RunnableConfig`` with those
  callbacks appended AFTER the caller's. It is PURE: it reads the lineage and copies the
  config by value, with no OpenTelemetry side effect — rooting and isolation are the
  backend's own concern in its writer.

The caller threads :attr:`RunTrace.config` onto the run (a graph door passes it so its
in-node model calls inherit the callbacks; a direct-LLM door passes it to the call) and
reads :attr:`RunTrace.context` to derive the configs of sub-runs that nest under the one
trace.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.monitoring import TraceContext, get_ambient_trace_context


@dataclass(frozen=True)
class RunTrace:
    """The resolved lineage of one run plus the per-invoke config that records it.

    ``context`` is the trace/span lineage the run's spans join; ``config`` is the caller's
    LangGraph run config (a ``RunnableConfig``-shaped mapping) copied by value with the
    backend's monitoring callbacks appended after the caller's own.
    """

    context: TraceContext
    config: dict[str, Any]


def resolve_trace_context(config: dict[str, Any] | None = None) -> TraceContext:
    """Resolve the one trace lineage for a run from ``config`` and the ambient deposit.

    Precedence: an explicit ``configurable['monitoring_trace_id']`` (with its optional
    ``monitoring_parent_span_id`` anchor) wins; else the ambient
    :func:`~tai42_contract.monitoring.get_ambient_trace_context` deposit; else a fresh
    32-hex root trace with no parent. Pure — reads only, mints no side effect.
    """
    configurable = (config or {}).get("configurable", {})
    trace_id = configurable.get("monitoring_trace_id")
    parent_span_id = configurable.get("monitoring_parent_span_id")
    if not trace_id:
        ambient = get_ambient_trace_context()
        if ambient is not None and ambient.trace_id:
            trace_id = ambient.trace_id
            if parent_span_id is None:
                parent_span_id = ambient.parent_span_id
        else:
            trace_id = uuid.uuid4().hex
    return TraceContext(trace_id=trace_id, parent_span_id=parent_span_id)


def bind_run_trace(config: dict[str, Any] | None = None) -> RunTrace:
    """Resolve the run's lineage and return the per-invoke config carrying its callbacks.

    Resolves the lineage with :func:`resolve_trace_context`, asks the active monitoring
    backend (``tai42_app.monitoring.active.writer``) for the callbacks under it, and
    returns a :class:`RunTrace` whose ``config`` is ``config`` copied by value — a fresh
    top-level mapping, a fresh ``configurable``, and a fresh ``callbacks`` list with the
    backend's callbacks appended after the caller's — so one base config can feed many
    parallel invocations without colliding. A backend error from
    ``get_monitoring_callbacks`` propagates loudly (dropping all tracing would be a silent
    degrade). No OpenTelemetry side effect.
    """
    source: dict[str, Any] = dict(config or {})
    context = resolve_trace_context(source)
    callbacks = tai42_app.monitoring.active.writer.get_monitoring_callbacks(context)
    bound: dict[str, Any] = dict(source)
    bound["configurable"] = dict(source.get("configurable", {}))
    bound["callbacks"] = [*source.get("callbacks", []), *callbacks]
    return RunTrace(context=context, config=bound)
