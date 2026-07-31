"""``LangfuseMonitoring`` — the single registered monitoring object."""

from __future__ import annotations

from tai42_contract.monitoring import ProjectConfig

from tai42_monitoring_langfuse.client_manager import LangfuseClientManager
from tai42_monitoring_langfuse.reader import LangfuseReader
from tai42_monitoring_langfuse.writer import LangfuseWriter


class LangfuseMonitoring:
    """A ``Monitoring`` backed by one or more Langfuse projects.

    ``writer.scope(public_key)`` switches the active project per block.
    """

    def __init__(
        self,
        *,
        projects: list[ProjectConfig],
        default_public_key: str,
    ) -> None:
        self._manager = LangfuseClientManager(projects, default_public_key)
        self._writer = LangfuseWriter(self._manager)
        self._reader = LangfuseReader(self._manager)

    def add_project(self, project: ProjectConfig) -> None:
        """Register an additional Langfuse project, selectable via ``writer.scope()``."""
        self._manager.add_project(project)

    @property
    def writer(self) -> LangfuseWriter:
        return self._writer

    @property
    def reader(self) -> LangfuseReader:
        return self._reader
