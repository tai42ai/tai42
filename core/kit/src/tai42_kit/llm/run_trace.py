"""The per-invoke run-trace binding: a run's config carrying the monitoring callbacks for its lineage.

Every model call carries monitoring through :func:`bind_run_trace`, so the active backend records
the call under the run's lineage no matter which agent drove it. The lineage itself comes from the
run-trace opener in :mod:`tai42_kit.monitoring.run_trace`.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.monitoring import TraceContext

from tai42_kit.llm.monitoring_callbacks import ChainPayloads, MonitoringCallbackHandler


def bind_run_trace(
    config: dict[str, Any] | None,
    context: TraceContext | None,
    *,
    grouping_nodes: frozenset[str] = frozenset(),
    chain_payloads: ChainPayloads = "references",
) -> dict[str, Any]:
    """``config`` copied by value, with the monitoring callbacks for ``context`` appended after the caller's own.

    The copy is a fresh top-level mapping, a fresh ``configurable`` and a fresh ``callbacks`` list, so one base
    config can feed many parallel invocations without colliding. A :class:`MonitoringCallbackHandler` bound to
    ``context`` is appended only when ``context`` is not ``None`` and the active writer records (``is_recording()``).

    ``grouping_nodes`` names the framework nodes that hold steps rather than being one.
    ``chain_payloads='references'`` (default) — each chain run records a manifest that
    references the record where each value first appeared, by identity; ``'producer'`` —
    the producer records chain-run inputs/outputs itself. A run declared through
    ``declared_chain_payload`` records the producer's ``build`` result in either mode. Model
    and tool runs are recorded in full either way.
    """
    source: dict[str, Any] = dict(config or {})
    callbacks: list[Any] = []
    if context is not None:
        writer = tai42_app.monitoring.active.writer
        if writer.is_recording():
            callbacks.append(
                MonitoringCallbackHandler(writer, context, grouping_nodes=grouping_nodes, chain_payloads=chain_payloads)
            )
    bound: dict[str, Any] = dict(source)
    bound["configurable"] = dict(source.get("configurable", {}))
    bound["callbacks"] = [*source.get("callbacks", []), *callbacks]
    return bound
