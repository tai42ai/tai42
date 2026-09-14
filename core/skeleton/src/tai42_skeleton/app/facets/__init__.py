"""Facet adapters mapping the concrete app across the facade's
``tai42_contract.app`` sub-protocols.

Each facet is a thin view bound to the owning :class:`~tai42_skeleton.app.server.TaiMCP`;
it forwards to the feature's impl collaborator (``ToolBinding``, ``AgentBinding``,
``BackendHolder``, the extension/monitoring registries, ``HttpSurface``, ...) so
the concrete app satisfies ``tai42_contract.app.TaiApp`` (every member partitioned
onto exactly one namespace). The facets are the app's SOLE feature/contract
surface; the concrete server additionally exposes a launch surface outside the
facade. The facets carry no state of their own.
"""

from __future__ import annotations

from .base import _Facet
from .http import HttpFacet
from .interactions import InteractionsFacet
from .lifecycle import AdminFacet, ConfigFacet, LifecycleFacet, SubAppFacet
from .presets import BackupFacet, PresetsFacet, ToolMetaFacet, VersioningFacet
from .registries import (
    AccountsFacet,
    AgentsFacet,
    BackendsFacet,
    ConnectorsFacet,
    ExtensionsFacet,
    MonitoringFacet,
    SandboxesFacet,
    StorageFacet,
    WebhookVerifiersFacet,
)
from .states import StatesFacet
from .tools import ToolsFacet

__all__ = [
    "AccountsFacet",
    "AdminFacet",
    "AgentsFacet",
    "BackendsFacet",
    "BackupFacet",
    "ConfigFacet",
    "ConnectorsFacet",
    "ExtensionsFacet",
    "HttpFacet",
    "InteractionsFacet",
    "LifecycleFacet",
    "MonitoringFacet",
    "PresetsFacet",
    "SandboxesFacet",
    "StatesFacet",
    "StorageFacet",
    "SubAppFacet",
    "ToolMetaFacet",
    "ToolsFacet",
    "VersioningFacet",
    "WebhookVerifiersFacet",
    "_Facet",
]
