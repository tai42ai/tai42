"""``LangfuseMonitoring`` — the single registered monitoring object."""

from __future__ import annotations

from tai42_kit.monitoring.otel import OtelWriter

from tai42_monitoring_langfuse.client_manager import LangfuseClientManager
from tai42_monitoring_langfuse.project import LangfuseProject
from tai42_monitoring_langfuse.reader import LangfuseReader


class LangfuseMonitoring:
    """A ``Monitoring`` that writes through the kit's OpenTelemetry writer and reads one Langfuse project.

    The project's ``source`` stamps every record (``deployment.environment.name``, which
    Langfuse reads as its environment) and scopes every read, so one value governs both.
    """

    def __init__(self, *, project: LangfuseProject) -> None:
        """Wire the writer and the reader over ``project``."""
        self._manager = LangfuseClientManager(project)
        self._writer = OtelWriter(resource_attributes={"deployment.environment.name": project.source})
        self._reader = LangfuseReader(self._manager)

    @property
    def writer(self) -> OtelWriter:
        """The write surface: the kit's OpenTelemetry writer."""
        return self._writer

    @property
    def reader(self) -> LangfuseReader:
        """The read surface (trace/observation queries)."""
        return self._reader
