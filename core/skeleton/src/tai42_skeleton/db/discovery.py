"""Discovery of the migration chains a TAI process owns.

The skeleton's own chain lives at a fixed packaged path and is registered
directly. A table-owning plugin declares its chain OPT-IN via the contract's
``migrations`` field (a package-relative directory); its component identity is its
pip distribution name. This module turns those declarations into
:class:`~tai42_kit.db.MigrationEntry` values the runner consumes — resolving a
plugin's packaged directory through ``importlib.resources`` and failing loudly when
a declared directory is absent from the installed package.
"""

from __future__ import annotations

import importlib.metadata
import importlib.resources
import re
from importlib.resources.abc import Traversable

from tai42_contract.plugins import PluginSpec
from tai42_kit.clients import PostgresConnectionSettings
from tai42_kit.db import MigrationDiscoveryError, MigrationEntry

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


def skeleton_entry(settings: PostgresConnectionSettings) -> MigrationEntry:
    """The skeleton chain as a runner entry against ``settings``."""
    return MigrationEntry(component=SKELETON_COMPONENT, migrations_dir=skeleton_migrations_dir(), settings=settings)


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


def plugin_migration_entry(spec: PluginSpec, settings: PostgresConnectionSettings) -> MigrationEntry | None:
    """A plugin's chain as a runner entry, or ``None`` when it declares none.

    The component is the plugin's distribution name (``spec.package``). The
    ``migrations`` path is resolved against the plugin's import root; the runner
    then enforces that the directory actually exists and holds a well-formed chain.
    """
    if spec.migrations is None:
        return None
    package = _import_package_for_distribution(spec.package)
    migrations_dir = importlib.resources.files(package).joinpath(*spec.migrations.split("/"))
    return MigrationEntry(component=spec.package, migrations_dir=migrations_dir, settings=settings)


async def installed_plugin_entries(settings: PostgresConnectionSettings) -> list[MigrationEntry]:
    """Runner entries for every marketplace-installed plugin that declares a chain.

    Reads the marketplace install-attribution store (the local record of every
    installed plugin and the exact ``PluginSpec`` it shipped). Empty when no
    marketplace store is configured — a deployment with no installed plugins has no
    plugin chains to migrate. Every plugin chain runs under the same migrator
    identity as the skeleton chain (``settings``).
    """
    from tai42_skeleton.marketplace.settings import marketplace_store_configured
    from tai42_skeleton.marketplace.store import MarketplaceInstallStore

    if not marketplace_store_configured():
        return []
    entries: list[MigrationEntry] = []
    for record in await MarketplaceInstallStore().list_installed():
        spec = PluginSpec.model_validate(record.spec)
        entry = plugin_migration_entry(spec, settings)
        if entry is not None:
            entries.append(entry)
    return entries


async def all_migration_entries(settings: PostgresConnectionSettings) -> list[MigrationEntry]:
    """Every chain this process is responsible for: the skeleton chain plus every
    installed plugin chain, all under the migrator identity ``settings``."""
    return [skeleton_entry(settings), *await installed_plugin_entries(settings)]
