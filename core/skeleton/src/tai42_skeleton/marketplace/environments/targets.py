"""Which environment each packaged plugin is built into, from the install store's rows.

A plugin whose every item is an ``mcp-server`` runs as its own process and gets its own
virtual environment; every other packaged plugin is imported by the serving interpreter and
is built into the shared plugin layer. A descriptor plugin (no package) is built nowhere. A
server directory no server package of the store's pins lives in is unnamed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from packaging.utils import canonicalize_name
from tai42_contract.plugins import PluginItemKind, PluginSpec

from tai42_skeleton.marketplace.environments.layout import BuildInput, EnvironmentDir, server_dirs
from tai42_skeleton.marketplace.resolve import spec_from_row
from tai42_skeleton.marketplace.store import InstallRecord


@dataclass(frozen=True)
class ServerCommand:
    """The ``command`` an ``mcp-server`` item of a server package names, by the item's name."""

    item: str
    command: str


@dataclass(frozen=True)
class PluginPin:
    """One packaged plugin as a build installs it.

    Its record inputs; for a github source, its archive URL; for a server package, the
    command each of its ``mcp-server`` items names, which its build must expose; the
    ``tai42-contract`` range its spec declares (``None``: none), by which the layer build
    leaves it out; and the import paths of its module items, which a build that leaves it
    out records.
    """

    input: BuildInput
    artifact_ref: str | None
    server_commands: tuple[ServerCommand, ...] = ()
    contract: str | None = None
    modules: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlacedPin:
    """A pin and whether it runs as its own server process."""

    pin: PluginPin
    runs_as_server: bool


@dataclass(frozen=True)
class Targets:
    """The pins every environment is built from: the layer's, and each server package's by normalized name."""

    layer: tuple[PluginPin, ...] = ()
    servers: dict[str, PluginPin] = field(default_factory=dict)


def runs_as_server(spec: PluginSpec) -> bool:
    """Whether ``spec`` is an ``mcp-server`` package: packaged, with every item an ``mcp-server``."""
    return spec.package is not None and {item.kind for item in spec.provides} == {PluginItemKind.MCP_SERVER}


def place(
    ref: str,
    spec: PluginSpec,
    version: str,
    source: str,
    artifact_ref: str | None,
    sha256: str | None,
) -> PlacedPin | None:
    """``spec`` pinned at ``version`` and the environment it belongs in; ``None`` for a descriptor plugin."""
    if spec.package is None:
        return None
    server = runs_as_server(spec)
    commands = tuple(
        ServerCommand(item=item.name, command=item.mcp.command)
        for item in spec.provides
        if server and item.mcp is not None and item.mcp.command is not None
    )
    pin = PluginPin(
        input=BuildInput(ref=ref, package=spec.package, version=version, source=source, sha256=sha256),
        artifact_ref=artifact_ref,
        server_commands=commands,
        contract=spec.contract,
        modules=tuple(item.module for item in spec.provides if item.module is not None),
    )
    return PlacedPin(pin=pin, runs_as_server=server)


def place_row(row: InstallRecord) -> PlacedPin | None:
    """The environment an install-store row belongs in; ``None`` for a descriptor plugin."""
    return place(row.ref, spec_from_row(row), row.version, row.source, row.artifact_ref, row.sha256)


def targets_of(placed: Iterable[PlacedPin | None]) -> Targets:
    """Group placed pins into the layer's and each server package's, skipping descriptor plugins."""
    layer: list[PluginPin] = []
    servers: dict[str, PluginPin] = {}
    for entry in placed:
        if entry is None:
            continue
        if entry.runs_as_server:
            servers[canonicalize_name(entry.pin.input.package)] = entry.pin
        else:
            layer.append(entry.pin)
    return Targets(layer=tuple(sorted(layer, key=lambda pin: pin.input)), servers=servers)


def store_targets(rows: Iterable[InstallRecord]) -> Targets:
    """The environments the install store's rows describe."""
    return targets_of(place_row(row) for row in rows)


def unnamed_server_dirs(plugins_dir: Path, targets: Targets) -> list[EnvironmentDir]:
    """Every server directory under ``plugins_dir`` that no server package of ``targets`` lives in."""
    return [environment for environment in server_dirs(plugins_dir) if environment.root.name not in targets.servers]
