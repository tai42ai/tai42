"""The writer's in-process count of everything it could not deliver."""

from __future__ import annotations

import threading

from tai42_contract.monitoring import MonitoringExportHealth


class ExportHealthCounters:
    """The six fields of ``MonitoringExportHealth`` behind one lock."""

    def __init__(self) -> None:
        """Start every count at zero."""
        self._lock = threading.Lock()
        self._spans_dropped = 0
        self._spans_failed = 0
        self._export_failures = 0
        self._attributes_dropped = 0
        self._records_failed = 0
        self._last_error: str | None = None

    def add_spans_dropped(self, count: int) -> None:
        """Count ``count`` spans evicted by a full export queue."""
        with self._lock:
            self._spans_dropped += count

    def add_export_failure(self, spans: int, error: str) -> None:
        """Count one failed export call of ``spans`` spans."""
        with self._lock:
            self._spans_failed += spans
            self._export_failures += 1
            self._last_error = error

    def add_attributes_dropped(self, count: int) -> None:
        """Count ``count`` attributes the SDK evicted from exported spans."""
        with self._lock:
            self._attributes_dropped += count

    def add_record_failed(self, error: str) -> None:
        """Count one record (or one field of a record) the writer could not build."""
        with self._lock:
            self._records_failed += 1
            self._last_error = error

    def snapshot(self) -> MonitoringExportHealth:
        """The counts as the contract model."""
        with self._lock:
            return MonitoringExportHealth(
                spans_dropped=self._spans_dropped,
                spans_failed=self._spans_failed,
                export_failures=self._export_failures,
                attributes_dropped=self._attributes_dropped,
                records_failed=self._records_failed,
                last_error=self._last_error,
            )
