"""The typed fleet ops the worker bus carries: each op's name, its fields, and what applying it changes.

The dispatcher owns this vocabulary. A publisher builds one of these models and the bus publishes
its dump; a receiver validates the delivered dict back into the model (an unknown op or a missing
field is refused before anything applies) and reports the op's declared ``effects`` to the
post-apply handlers. ``prefix`` is the only defaulted field, so a dump that excludes defaults is
the exact wire dict: the tag is always present, ``prefix`` only when set.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from tai42_contract.backend import FleetOpEffect

__all__ = [
    "FLEET_OP_ADAPTER",
    "ClearTemplateCache",
    "DeploymentKeysChanged",
    "DeregisterMcp",
    "EvictTemplate",
    "FleetOp",
    "ListFailedMcps",
    "Recycle",
    "ReloadConfig",
    "ReloadFailedMcps",
    "ReloadMcp",
    "ReloadTool",
    "RemoveTool",
    "ReprobeFailedMcps",
    "fleet_op_wire",
]

_REGISTRY: frozenset[FleetOpEffect] = frozenset({FleetOpEffect.registry})
_TEMPLATES: frozenset[FleetOpEffect] = frozenset({FleetOpEffect.templates})
_NONE: frozenset[FleetOpEffect] = frozenset()


class _FleetOp(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    effects: ClassVar[frozenset[FleetOpEffect]]


class ReloadConfig(_FleetOp):
    """Re-read the persisted config and rebuild the registries."""

    op: Literal["reload_config"]
    effects: ClassVar[frozenset[FleetOpEffect]] = _REGISTRY


class ReloadMcp(_FleetOp):
    """Reload one mounted MCP server by title."""

    op: Literal["reload_mcp"]
    title: str
    effects: ClassVar[frozenset[FleetOpEffect]] = _REGISTRY


class DeregisterMcp(_FleetOp):
    """Drop one mounted MCP server by title."""

    op: Literal["deregister_mcp"]
    title: str
    effects: ClassVar[frozenset[FleetOpEffect]] = _REGISTRY


class ReloadTool(_FleetOp):
    """Rebind one tool of a kind by name."""

    op: Literal["reload_tool"]
    kind: str
    name: str
    effects: ClassVar[frozenset[FleetOpEffect]] = _REGISTRY


class RemoveTool(_FleetOp):
    """Unbind one tool of a kind by name."""

    op: Literal["remove_tool"]
    kind: str
    name: str
    effects: ClassVar[frozenset[FleetOpEffect]] = _REGISTRY


class ReloadFailedMcps(_FleetOp):
    """Retry every MCP server that failed to mount."""

    op: Literal["reload_failed_mcps"]
    effects: ClassVar[frozenset[FleetOpEffect]] = _REGISTRY


class ReprobeFailedMcps(_FleetOp):
    """Re-probe the named MCP servers that failed to mount, mounting each that answers now."""

    op: Literal["reprobe_failed_mcps"]
    titles: list[str]
    effects: ClassVar[frozenset[FleetOpEffect]] = _REGISTRY


class ListFailedMcps(_FleetOp):
    """Report the MCP servers that failed to mount; changes nothing."""

    op: Literal["list_failed_mcps"]
    effects: ClassVar[frozenset[FleetOpEffect]] = _NONE


class EvictTemplate(_FleetOp):
    """Drop one compiled template, or every template under ``path`` when ``prefix`` is set."""

    op: Literal["evict_template"]
    path: str
    prefix: bool = False
    effects: ClassVar[frozenset[FleetOpEffect]] = _TEMPLATES


class ClearTemplateCache(_FleetOp):
    """Drop the whole compiled-template cache."""

    op: Literal["clear_template_cache"]
    effects: ClassVar[frozenset[FleetOpEffect]] = _TEMPLATES


class Recycle(_FleetOp):
    """Gracefully exit this worker after its reply ships; changes nothing it keeps."""

    op: Literal["recycle"]
    effects: ClassVar[frozenset[FleetOpEffect]] = _NONE


class DeploymentKeysChanged(_FleetOp):
    """A worker joined the bus: withdraw the applied stored keys the fleet's deployment now owns.

    Carries no names: each receiver re-reads the census, so it judges by every row as it stands.
    A receiver that withdrew a key rebuilds its serving generation through the config reload it
    applies (that reload carries the registry effect), and replies with the withdrawn names and
    the recycle-class ones among them it still runs on. The op itself changes nothing, so it
    declares no effects; its reload does.
    """

    op: Literal["deployment_keys_changed"]
    effects: ClassVar[frozenset[FleetOpEffect]] = _NONE


FleetOp = Annotated[
    ReloadConfig
    | ReloadMcp
    | DeregisterMcp
    | ReloadTool
    | RemoveTool
    | ReloadFailedMcps
    | ReprobeFailedMcps
    | ListFailedMcps
    | EvictTemplate
    | ClearTemplateCache
    | Recycle
    | DeploymentKeysChanged,
    Field(discriminator="op"),
]

FLEET_OP_ADAPTER: TypeAdapter[FleetOp] = TypeAdapter(FleetOp)


def fleet_op_wire(op: FleetOp) -> dict[str, Any]:
    """The wire dict a fleet op travels as: its fields, defaults omitted."""
    return op.model_dump(exclude_defaults=True)
