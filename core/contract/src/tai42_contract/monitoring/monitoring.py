"""The single registered monitoring object: a writer + a reader."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tai42_contract.monitoring.models import ProjectConfig
from tai42_contract.monitoring.reader import MonitoringReader
from tai42_contract.monitoring.writer import MonitoringWriter


@runtime_checkable
class Monitoring(Protocol):
    """One backend, two faces.

    ``writer`` is always present (every backend can emit). ``reader`` always
    returns an object; for a read-incapable backend its METHODS raise
    ``MonitoringReadNotSupportedError`` (the property access does not).
    """

    @property
    def writer(self) -> MonitoringWriter: ...

    @property
    def reader(self) -> MonitoringReader: ...

    def add_project(self, project: ProjectConfig) -> None:
        """Register an additional project, selectable via
        ``writer.scope(project.public_key)``.

        Mirrors a single-backend multi-project switch: an in-process component
        sharing this backend emits to its own project. A backend with no
        multi-project notion may no-op it (as ``scope`` does); a real failure
        must raise (cross-project leak risk), never be swallowed.
        """
        ...
