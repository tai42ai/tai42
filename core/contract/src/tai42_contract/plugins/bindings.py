"""Item-kind → manifest wiring: the kind enum, the binding model, and the binding tables."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class PluginItemKind(StrEnum):
    """Kind of one installable item a plugin provides.

    The values are the ecosystem's item-kind vocabulary (the same words the
    catalog and the marketplace facets use). ``KIND_MANIFEST_BINDINGS`` maps
    every member onto its manifest wiring; an unknown kind in a spec is a
    loud validation reject, never a skipped row.
    """

    TOOL = "tool"
    AGENT = "agent"
    EXTENSION = "extension"
    CONNECTOR = "connector"
    CHANNEL = "channel"
    BACKEND = "backend"
    STORAGE = "storage"
    MONITORING = "monitoring"
    WEBHOOK_VERIFIER = "webhook-verifier"
    CONFIG = "config"
    IDENTITY = "identity"
    STUDIO_PLUGIN = "studio-plugin"
    ROUTER = "router"
    MIDDLEWARE = "middleware"
    MCP_SERVER = "mcp-server"
    SANDBOX = "sandbox"


class ManifestBinding(BaseModel):
    """How one provided item kind wires into a ``Manifest``.

    ``field`` names the manifest field an installer patches for an item of
    this kind; ``mode`` is the patch shape; ``payload`` is the axis the item
    carries — ``module`` (the item names an import path) or ``data`` (the item
    carries its kind's declarative block):

    - ``config_row`` — append a config row (``tools``/``agents``) whose
      ``module`` is the item's module.
    - ``module_list`` — append the item's module to a plain module list.
    - ``scalar_module`` — set a single-module slot; the slot holds ONE module,
      so a second plugin claiming an occupied slot is a conflict the caller
      must reject loudly.
    - ``package_list`` — append the plugin's DISTRIBUTION name (not the item's
      module) to a package list (``studio_plugins``).
    - ``mcp_entry`` — append one ``{title: item.name, config: item.mcp}`` object
      to the manifest ``mcp`` list; uninstall removes the entry by title.
    - ``descriptor_entry`` — append the item's ``provider`` descriptor to the
      manifest ``connectors`` list; uninstall removes by ``provider.id``.
    - ``env_selected`` — no manifest field: the kind is selected through the
      environment (a ``config`` provider is named by ``TAI_CONFIG_MODE`` and
      imported by the config seam), so ``field`` is ``None``.

    The data modes (``mcp_entry``, ``descriptor_entry``) carry ``payload ==
    "data"``; every other mode carries ``payload == "module"``.
    """

    model_config = ConfigDict(frozen=True)

    field: str | None
    mode: Literal[
        "config_row", "module_list", "scalar_module", "package_list", "mcp_entry", "descriptor_entry", "env_selected"
    ]
    payload: Literal["module", "data"]

    @model_validator(mode="after")
    def _field_iff_manifest_wired(self) -> ManifestBinding:
        if (self.field is None) != (self.mode == "env_selected"):
            raise ValueError("field must be None exactly when mode is 'env_selected'")
        return self

    @model_validator(mode="after")
    def _payload_matches_mode(self) -> ManifestBinding:
        if (self.mode in ("mcp_entry", "descriptor_entry")) != (self.payload == "data"):
            raise ValueError("payload must be 'data' exactly when mode is 'mcp_entry' or 'descriptor_entry'")
        return self


# The single source of kind→manifest wiring, consumed by the skeleton
# installer (patch/unpatch) and the marketplace registry (item classification).
KIND_MANIFEST_BINDINGS: Mapping[PluginItemKind, ManifestBinding] = MappingProxyType(
    {
        PluginItemKind.TOOL: ManifestBinding(field="tools", mode="config_row", payload="module"),
        PluginItemKind.AGENT: ManifestBinding(field="agents", mode="config_row", payload="module"),
        PluginItemKind.EXTENSION: ManifestBinding(field="extensions_modules", mode="module_list", payload="module"),
        PluginItemKind.CONNECTOR: ManifestBinding(field="connectors", mode="descriptor_entry", payload="data"),
        PluginItemKind.CHANNEL: ManifestBinding(field="channel_modules", mode="module_list", payload="module"),
        PluginItemKind.BACKEND: ManifestBinding(field="backend_module", mode="scalar_module", payload="module"),
        PluginItemKind.STORAGE: ManifestBinding(field="storage_module", mode="scalar_module", payload="module"),
        PluginItemKind.MONITORING: ManifestBinding(field="monitoring_module", mode="scalar_module", payload="module"),
        PluginItemKind.WEBHOOK_VERIFIER: ManifestBinding(
            field="webhook_verifier_modules", mode="module_list", payload="module"
        ),
        PluginItemKind.CONFIG: ManifestBinding(field=None, mode="env_selected", payload="module"),
        PluginItemKind.IDENTITY: ManifestBinding(field="lifecycle_modules", mode="module_list", payload="module"),
        PluginItemKind.STUDIO_PLUGIN: ManifestBinding(field="studio_plugins", mode="package_list", payload="module"),
        PluginItemKind.ROUTER: ManifestBinding(field="routers_modules", mode="module_list", payload="module"),
        PluginItemKind.MIDDLEWARE: ManifestBinding(field="middlewares_modules", mode="module_list", payload="module"),
        PluginItemKind.MCP_SERVER: ManifestBinding(field="mcp", mode="mcp_entry", payload="data"),
        PluginItemKind.SANDBOX: ManifestBinding(field="sandbox_module", mode="scalar_module", payload="module"),
    }
)


# The declarative block a data item of each kind carries — the field the item
# sets in place of ``module``. Its keys are exactly the data kinds.
DATA_BLOCK_BY_KIND: Mapping[PluginItemKind, str] = MappingProxyType(
    {
        PluginItemKind.MCP_SERVER: "mcp",
        PluginItemKind.CONNECTOR: "provider",
    }
)


def data_kinds() -> frozenset[PluginItemKind]:
    """The item kinds whose binding carries ``payload == "data"``.

    These are the kinds a declarative (no-``module``) item is built for. Derived
    from ``KIND_MANIFEST_BINDINGS`` so the payload table stays the one source.
    """
    return frozenset(kind for kind, binding in KIND_MANIFEST_BINDINGS.items() if binding.payload == "data")
