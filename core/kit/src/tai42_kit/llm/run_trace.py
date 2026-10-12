"""The per-invoke run-trace seam: resolve a run's trace lineage and bind its callbacks.

Every FEATURE's model call carries monitoring through ONE building block here, so the
active backend records the call under the run's trace no matter which agent drove it.

* :func:`resolve_trace_context` resolves the ONE lineage a run traces under — an
  EXPLICITLY propagated ``MONITORING_TRACE_ID_KEY`` (a caller pinned it, e.g. a monitored
  driver invoking the run directly) wins; else the AMBIENT context a driver deposited
  when it drives this run as a node, so the run JOINS that trace instead of orphaning
  into a fresh one; else a freshly minted 32-hex root (the standalone default).
* :func:`bind_run_trace` resolves that lineage and returns the per-invoke
  ``RunnableConfig`` with the kit's :class:`MonitoringCallbackHandler` (recording through
  the active monitoring writer) appended AFTER the caller's callbacks — only when the
  writer records anything, so a deployment without monitoring pays no callback cost. It
  copies the config by value and opens no span itself.

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
from tai42_contract.errors import ErrorKind
from tai42_contract.monitoring import (
    MONITORING_PARENT_SPAN_ID_KEY,
    MONITORING_TRACE_ID_KEY,
    TraceContext,
    get_ambient_trace_context,
    is_span_id,
    is_trace_id,
)

from tai42_kit.llm.monitoring_callbacks import ChainPayloads, MonitoringCallbackHandler


class InvalidRunTraceError(ValueError):
    """A run's config names an explicit trace id or parent anchor that no monitoring writer can record under.

    The caller's input is refused before the run starts, naming the configurable key and the value it carried.
    """

    __tai_error_kind__ = ErrorKind.BAD_INPUT


@dataclass(frozen=True)
class RunTrace:
    """The resolved lineage of one run plus the per-invoke config that records it.

    ``context`` is the trace/span lineage the run's spans join; ``config`` is the caller's
    LangGraph run config (a ``RunnableConfig``-shaped mapping) copied by value with the
    monitoring callback handler appended after the caller's own callbacks.
    """

    context: TraceContext
    config: dict[str, Any]


def resolve_trace_context(config: dict[str, Any] | None = None) -> TraceContext:
    """Resolve the one trace lineage for a run from ``config`` and the ambient deposit.

    Precedence: an explicit ``configurable[MONITORING_TRACE_ID_KEY]`` (with its optional
    ``MONITORING_PARENT_SPAN_ID_KEY`` anchor) wins; else the ambient
    :func:`~tai42_contract.monitoring.get_ambient_trace_context` deposit; else a fresh
    32-hex root trace with no parent. Pure — reads only, mints no side effect.

    An explicit trace id that is not 32 lower-hex (not all zeros), or a caller's parent anchor that is not 16
    lower-hex (not all zeros), is refused with :class:`InvalidRunTraceError`: a run never proceeds under a lineage
    no writer can record. An empty explicit trace id reads as absent.
    """
    configurable = (config or {}).get("configurable", {})
    trace_id = configurable.get(MONITORING_TRACE_ID_KEY)
    parent_span_id = configurable.get(MONITORING_PARENT_SPAN_ID_KEY)
    if trace_id and not is_trace_id(trace_id):
        raise InvalidRunTraceError(
            f"configurable[{MONITORING_TRACE_ID_KEY!r}] must be a 32-character lowercase hex trace id that is not all "
            f"zeros; got {trace_id!r}"
        )
    if parent_span_id is not None and not is_span_id(parent_span_id):
        raise InvalidRunTraceError(
            f"configurable[{MONITORING_PARENT_SPAN_ID_KEY!r}] must be a 16-character lowercase hex span id that is not "
            f"all zeros; got {parent_span_id!r}"
        )
    if not trace_id:
        ambient = get_ambient_trace_context()
        if ambient is not None and ambient.trace_id:
            trace_id = ambient.trace_id
            if parent_span_id is None:
                parent_span_id = ambient.parent_span_id
        else:
            trace_id = uuid.uuid4().hex
    return TraceContext(trace_id=trace_id, parent_span_id=parent_span_id)


def bind_run_trace(
    config: dict[str, Any] | None = None,
    *,
    grouping_nodes: frozenset[str] = frozenset(),
    chain_payloads: ChainPayloads = "references",
) -> RunTrace:
    """Resolve the run's lineage and return the per-invoke config carrying the monitoring callbacks.

    Resolves the lineage with :func:`resolve_trace_context` (its :class:`InvalidRunTraceError` propagates) and
    returns a :class:`RunTrace` whose ``config`` is ``config`` copied by value — a fresh top-level mapping, a fresh
    ``configurable``, and a fresh ``callbacks`` list — so one base config can feed many
    parallel invocations without colliding. When the active writer records
    (``is_recording()``), a :class:`MonitoringCallbackHandler` bound to the lineage is
    appended after the caller's callbacks; otherwise nothing is appended.

    ``grouping_nodes`` names the framework nodes that hold steps rather than being one.
    ``chain_payloads='references'`` (default) — each chain run records a manifest that
    references the record where each value first appeared, by identity; ``'producer'`` —
    the producer records chain-run inputs/outputs itself. A run declared through
    ``declared_chain_payload`` records the producer's ``build`` result in either mode. Model
    and tool runs are recorded in full either way.
    """
    source: dict[str, Any] = dict(config or {})
    context = resolve_trace_context(source)
    writer = tai42_app.monitoring.active.writer
    callbacks: list[Any] = []
    if writer.is_recording():
        callbacks.append(
            MonitoringCallbackHandler(writer, context, grouping_nodes=grouping_nodes, chain_payloads=chain_payloads)
        )
    bound: dict[str, Any] = dict(source)
    bound["configurable"] = dict(source.get("configurable", {}))
    bound["callbacks"] = [*source.get("callbacks", []), *callbacks]
    return RunTrace(context=context, config=bound)
