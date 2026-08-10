"""Vendor-neutral monitoring contract.

Generic across LLM-observability backends, modeled on OpenTelemetry span
semantics, cost/tokens/input/output optional, write/read as separate
capabilities so emit-only backends still fit. Concrete backends are implemented
elsewhere; this contract names and assumes none.
"""

from __future__ import annotations

from tai42_contract.monitoring.errors import (
    MonitoringError,
    MonitoringReadNotSupportedError,
    TraceNotFoundError,
)
from tai42_contract.monitoring.models import (
    DEFAULT_LEVEL,
    MetricsFilter,
    MetricsResult,
    MetricsRow,
    MetricsView,
    MonitoringFilter,
    MonitoringLevel,
    MonitoringObservation,
    MonitoringTrace,
    OrderBy,
    ProjectConfig,
    Span,
    SpanKind,
    SpanWindowItem,
    TraceContext,
)
from tai42_contract.monitoring.monitoring import Monitoring
from tai42_contract.monitoring.reader import MonitoringReader
from tai42_contract.monitoring.writer import MonitoringWriter

__all__ = [
    "DEFAULT_LEVEL",
    "MetricsFilter",
    "MetricsResult",
    "MetricsRow",
    "MetricsView",
    "Monitoring",
    "MonitoringError",
    "MonitoringFilter",
    "MonitoringLevel",
    "MonitoringObservation",
    "MonitoringReadNotSupportedError",
    "MonitoringReader",
    "MonitoringTrace",
    "MonitoringWriter",
    "OrderBy",
    "ProjectConfig",
    "Span",
    "SpanKind",
    "SpanWindowItem",
    "TraceContext",
    "TraceNotFoundError",
]
