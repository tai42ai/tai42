"""Response models for the manifest operations: the preserved manifest view and the MCP env, status and tool reads."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, RootModel
from tai42_contract.manifest import ExtensionElement


class ManifestExtensionMap(BaseModel):
    """One ``tools`` or ``mcp`` entry's ``extensions`` map, keyed by the entry's ``title``.

    ``extensions`` maps a tool base-name to its list of combos in the contract's normalized shape; a combo element
    is an extension name or a ``{"name", "config"}`` mapping whose config values keep their ``!ENV`` markers.
    """

    title: str
    extensions: dict[str, list[list[ExtensionElement]]]


class ManifestExtensionSettings(BaseModel):
    """The extension settings the PRESERVED manifest declares, ``!ENV`` markers intact.

    ``tools`` and ``mcp`` list only the entries that carry a non-empty ``extensions`` map, in document order;
    ``api_tools`` is the ``api_tools.extensions`` map.
    """

    extensions_modules: list[str]
    backend_module: str | None
    tools: list[ManifestExtensionMap]
    mcp: list[ManifestExtensionMap]
    api_tools: dict[str, list[list[ExtensionElement]]]


class PreservedManifestView(BaseModel):
    """The PRESERVED persisted manifest's MCP section, user tools and extension settings.

    The ``mcp`` entries carry their raw ``!ENV ${KEY}`` markers intact (unresolved config entries, NOT a
    resolved ``TaiMCPConfig``), and so do the extension settings, so no secret value ever leaves on the wire.
    """

    mcp: list[dict[str, Any]]
    user_tools: list[str]
    extension_settings: ManifestExtensionSettings


class McpEnvRef(BaseModel):
    """One ``!ENV ${VAR[:default]}`` marker ref carried by the manifest's MCP section — NAMES and BOOLEANS only.

    ``pointer`` is the marker leaf's json-pointer; ``set`` is whether the var is present in the effective
    environment.
    """

    var: str
    pointer: str
    has_default: bool
    set: bool


class McpEnvRefList(RootModel[list[McpEnvRef]]):
    """The manifest MCP section's ``!ENV`` marker refs, in document order."""


class FailedMcp(BaseModel):
    """An MCP server skipped by the viability check — title, coarse status, and credential-free failure detail.

    ``category`` is the coarse, credential-free cause (``preset_store`` when the preset
    store could not be read while the server's tools were being bound, ``auth`` for a
    401/403, ``unreachable`` for a transport error or timeout, ``error`` otherwise); ``message``
    is the redacted exception message (URL-embedded credentials stripped); ``http_status``
    is the HTTP status the failure carried, or ``null`` for a pure transport failure.
    """

    title: str
    status: str
    category: str
    message: str
    http_status: int | None


class McpHealth(BaseModel):
    """One MCP server's passive dispatch health.

    ``last_error`` carries a ``{type, message, at}`` block when a failure has been recorded, else ``null``;
    ``last_success`` / ``failing_since`` are ISO-8601 timestamps or ``null``.
    """

    last_success: str | None
    last_error: dict[str, str] | None
    consecutive_failures: int
    failing_since: str | None


class McpStatusSnapshot(BaseModel):
    """The live MCP-binding snapshot.

    ``bound`` maps each server title to its bound tool names, ``failed`` lists the skipped servers, and
    ``health`` maps every bound and failed title to its dispatch-health block.
    """

    bound: dict[str, list[str]]
    failed: list[FailedMcp]
    health: dict[str, McpHealth]


class McpAdvertisedTool(BaseModel):
    """One tool an MCP server advertises.

    ``name`` is its served name and ``title``/``description`` the server's; ``selected`` says whether the
    entry's selection admits it, ``bound`` whether the live binding carries it, and ``unbound_reason`` why a
    selected tool is not bound (``None`` otherwise).
    """

    name: str
    title: str | None
    description: str | None
    selected: bool
    bound: bool
    unbound_reason: (
        Literal["server_failed", "not_loaded", "preset_conflict", "unusable_schema", "not_listed_at_bind"] | None
    )


class McpAdvertisedTools(BaseModel):
    """A server's advertised tools with the entry's stored selection lists.

    ``unadvertised`` holds the names of the set list (``include`` when non-empty, else ``exclude``) that the
    server no longer advertises.
    """

    title: str
    include: list[str]
    exclude: list[str]
    tools: list[McpAdvertisedTool]
    unadvertised: list[str]
