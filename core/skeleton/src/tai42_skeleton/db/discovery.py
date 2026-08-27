"""Discovery of the migration chains a TAI process owns.

The skeleton's own chain lives at a fixed packaged path and is registered
directly. A table-owning plugin declares its chain OPT-IN via the contract's
``migrations`` field (a package-relative directory); its component identity is its
pip distribution name. This module turns those declarations into
:class:`~tai42_kit.db.MigrationEntry` values the runner consumes — resolving a
plugin's packaged directory through ``importlib.resources`` and failing loudly when
a declared directory is absent from the installed package.

Each entry's connection is the migrator (DDL-privileged) identity of the database
its component is bound to, resolved through the central registry
(:func:`~tai42_kit.db.component_migrator_settings`).
"""

from __future__ import annotations

import importlib.metadata
import importlib.resources
import logging
import re
from dataclasses import dataclass
from importlib.resources.abc import Traversable

from tai42_contract.plugins import PluginSpec
from tai42_kit.db import (
    MigrationDiscoveryError,
    MigrationEntry,
    component_binding_declared,
    component_migrator_settings,
)

logger = logging.getLogger(__name__)

# The skeleton's chain is component ``skeleton`` — the identity in the history
# table, fixed once and forever.
SKELETON_COMPONENT = "skeleton"

# The skeleton chain's packaged directory, relative to the ``tai42_skeleton``
# import root.
_SKELETON_MIGRATIONS_SUBPATH = ("sql", "migrations")


def skeleton_migrations_dir() -> Traversable:
    """The packaged directory holding the skeleton chain's SQL files."""
    root = importlib.resources.files("tai42_skeleton")
    return root.joinpath(*_SKELETON_MIGRATIONS_SUBPATH)


def skeleton_entry() -> MigrationEntry:
    """The skeleton chain as a runner entry against the skeleton component's bound
    migrator identity."""
    return MigrationEntry(
        component=SKELETON_COMPONENT,
        migrations_dir=skeleton_migrations_dir(),
        settings=component_migrator_settings(SKELETON_COMPONENT),
    )


def _normalize_dist(name: str) -> str:
    """PEP 503 normalized distribution name, for matching across the hyphen /
    underscore / dot variants pip treats as one project."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _import_package_for_distribution(distribution: str) -> str:
    """The top-level import package a pip distribution installs.

    A plugin's ``migrations`` path is package-relative, so it resolves against the
    plugin's import root — which is not always the pip distribution name (hyphens
    become underscores, and a distribution may namespace its package). Resolved
    from installed metadata; a distribution that is not installed, or maps to no /
    more than one top-level import package, is a loud discovery failure rather than
    a guess.
    """
    target = _normalize_dist(distribution)
    mapping = importlib.metadata.packages_distributions()
    packages = sorted({pkg for pkg, dists in mapping.items() if any(_normalize_dist(d) == target for d in dists)})
    if not packages:
        raise MigrationDiscoveryError(
            f"cannot resolve the import package for distribution {distribution!r}: it is not installed, or ships no "
            "top-level import package"
        )
    if len(packages) > 1:
        raise MigrationDiscoveryError(
            f"distribution {distribution!r} maps to multiple import packages {packages}; its migrations directory is "
            "ambiguous"
        )
    return packages[0]


@dataclass(frozen=True)
class _ChainSkip:
    """A declared chain whose ``migrations_component`` override binding is unset: a
    DISTINCT surfaced skip, never the silent ``None`` no-migrations outcome.

    ``component`` is the override identity whose ``TAI_DB_BINDING_*`` the operator
    must declare before its store migrates anywhere — carried so the visible skip
    line names the chain that did not run."""

    component: str


def _log_chain_skip(skip: _ChainSkip) -> None:
    """Surface a skipped override chain as a visible line — WHICH chain did not run
    and WHY — so an optional feature's store never silently migrates into the default
    database by fallback."""
    logger.warning(
        "db migrate: skipping the %r migration chain — its migrations-component database "
        "binding (TAI_DB_BINDING_*) is not declared; set it so this component's store migrates "
        "to the intended database.",
        skip.component,
    )


def _plugin_chain(spec: PluginSpec) -> MigrationEntry | _ChainSkip | None:
    """Resolve a plugin's declared chain to one of THREE distinct outcomes: an entry
    to run, a :class:`_ChainSkip` (an override whose component binding is unset —
    surfaced, never a silent ``None``), or ``None`` when the plugin declares no chain.

    ``migrations_component`` (when set) names the database component the chain
    migrates instead of the distribution; it flows into BOTH the entry's ``component``
    history identity AND its ``settings`` connection — one without the other migrates
    the right identity into the wrong database. An override runs ONLY while its
    binding is EXPLICITLY declared; unset, the chain is skipped rather than run under
    the default-database fallback. With no override the component is the distribution
    name, byte-identical to prior behavior.
    """
    if spec.migrations is None:
        return None
    # ``migrations`` requires a ``package`` (a cross-field contract rule — a
    # descriptor-only plugin owns no packaged SQL), so a non-None ``migrations``
    # here guarantees a non-None ``package``.
    if spec.package is None:
        raise MigrationDiscoveryError("migrations require a package")
    # The component identity the chain migrates under: the declared override, else the
    # distribution name. Resolved BEFORE the package/directory read so a skipped
    # override chain never needs the feature package's SQL resolved.
    component = spec.migrations_component or spec.package
    if spec.migrations_component is not None and component_binding_declared(component) is None:
        return _ChainSkip(component=component)
    package = _import_package_for_distribution(spec.package)
    migrations_dir = importlib.resources.files(package).joinpath(*spec.migrations.split("/"))
    return MigrationEntry(
        component=component,
        migrations_dir=migrations_dir,
        settings=component_migrator_settings(component),
    )


def plugin_migration_entry(spec: PluginSpec) -> MigrationEntry | None:
    """A plugin's chain as a runner entry, or ``None`` when it declares none OR its
    ``migrations_component`` override binding is unset (a skip surfaced by a visible
    line — an undeclared binding never migrates into the default database).

    The component is the declared ``migrations_component`` when set, else the plugin's
    distribution name (``spec.package``); its connection is that component's bound
    migrator identity. The ``migrations`` path is resolved against the plugin's import
    root; the runner then enforces that the directory actually exists and holds a
    well-formed chain.
    """
    outcome = _plugin_chain(spec)
    if isinstance(outcome, _ChainSkip):
        _log_chain_skip(outcome)
        return None
    return outcome


async def installed_plugin_entries() -> list[MigrationEntry]:
    """Runner entries for every marketplace-installed plugin that declares a chain.

    Reads the marketplace install-attribution store (the local record of every
    installed plugin and the exact ``PluginSpec`` it shipped). Empty when the
    skeleton database is not configured — a deployment with no store has no plugin
    chains to migrate. Each plugin chain runs under its own component's bound
    migrator identity.
    """
    from tai42_kit.db import component_store_configured

    from tai42_skeleton.marketplace.store import MarketplaceInstallStore

    if not component_store_configured(SKELETON_COMPONENT):
        return []
    entries: list[MigrationEntry] = []
    for record in await MarketplaceInstallStore().list_installed():
        spec = PluginSpec.model_validate(record.spec)
        outcome = _plugin_chain(spec)
        if isinstance(outcome, _ChainSkip):
            # DISTINCT from the ``None`` no-migrations outcome: surface the skip and
            # omit the chain, so ``tai db migrate`` reports WHICH declared chain did
            # not run rather than silently dropping it.
            _log_chain_skip(outcome)
            continue
        if outcome is not None:
            entries.append(outcome)
    return entries


async def all_migration_entries() -> list[MigrationEntry]:
    """Every chain this process is responsible for: the skeleton chain plus every
    installed plugin chain, each under its component's bound migrator identity."""
    return [skeleton_entry(), *await installed_plugin_entries()]
