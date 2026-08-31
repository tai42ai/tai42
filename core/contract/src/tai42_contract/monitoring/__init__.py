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
    TRACE_PREVIEW_MAX_CHARS,
    MetricsFilter,
    MetricsResult,
    MetricsRow,
    MetricsView,
    MonitoringFilter,
    MonitoringLevel,
    MonitoringObservation,
    MonitoringTrace,
    MonitoringTraceSummary,
    OrderBy,
    ProjectConfig,
    RunAttribution,
    Span,
    SpanKind,
    SpanWindowItem,
    TraceContext,
    preview,
)
from tai42_contract.monitoring.monitoring import Monitoring
from tai42_contract.monitoring.reader import MonitoringReader
from tai42_contract.monitoring.writer import (
    RUN_ATTRIBUTION_TRACE_NAME,
    RUN_VERSION_METADATA_KEY,
    MonitoringWriter,
    attribute_run,
)

__all__ = [
    "DEFAULT_LEVEL",
    "RUN_ATTRIBUTION_TRACE_NAME",
    "RUN_VERSION_METADATA_KEY",
    "TRACE_PREVIEW_MAX_CHARS",
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
    "MonitoringTraceSummary",
    "MonitoringWriter",
    "OrderBy",
    "ProjectConfig",
    "RunAttribution",
    "Span",
    "SpanKind",
    "SpanWindowItem",
    "TraceContext",
    "TraceNotFoundError",
    "attribute_run",
    "preview",
]
