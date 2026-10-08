"""The OpenTelemetry writer every monitoring backend records through (needs the ``monitoring`` extra)."""

from __future__ import annotations

from tai42_kit.monitoring.otel.writer import OtelWriter

__all__ = ["OtelWriter"]
