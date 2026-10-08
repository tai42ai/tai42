"""Errors of the monitoring encoder (importable without the ``monitoring`` extra)."""

from __future__ import annotations


class MonitoringEncodeError(TypeError):
    """A value cannot be encoded for monitoring, or carries a reserved marker object."""
