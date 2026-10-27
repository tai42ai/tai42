"""Discovery of the migration chains a TAI process owns.

The skeleton's own chain lives at a fixed packaged path and is registered
directly. A table-owning plugin declares its chain OPT-IN via the contract's
``migrations`` field (a package-relative directory); its component identity is its
pip distribution name. ``tai db migrate`` discovers the chain of every INSTALLED plugin
whose packages its metadata records — every distribution in the environment or the plugins
prefix that packages a ``tai-plugin.yml``, read from installed metadata and never imported,
whether or not a manifest loads it, so the schema follows what is installed — plus the chain
of every plugin the persisted manifest loads. An editable install served by an import hook
records its packages nowhere, so the enumeration cannot list them: such a distribution is
named in the result (:class:`UnlocatablePackages`), and its chain is discovered once a
manifest loads a module of it. This module turns those declarations into
:class:`~tai42_kit.db.MigrationEntry` values, resolving a plugin's packaged directory against
the directory its spec is installed in, and failing loudly when a spec is malformed.

One resolution, :func:`plugin_chain`, serves both consumers; only the connection identity
differs. ``tai db migrate`` runs each entry under the migrator (DDL-privileged) identity of
the database its component is bound to (:func:`~tai42_kit.db.component_migrator_settings`).
The boot-time schema gate (:mod:`tai42_skeleton.db.boot_gate`) checks the chains of the
plugins the manifest loads (:func:`manifest_plugin_specs`) on each component's runtime store
identity (:func:`~tai42_kit.db.component_store_settings`); every one of those is installed,
and migrate resolves those same plugins through the same function, so migrate has applied
each chain boot verifies.
"""

from __future__ import annotations

import importlib.metadata
import importlib.resources
import importlib.util
import logging
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from importlib.resources.abc import Traversable, TraversableResources
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError
from tai42_contract.plugins import PluginSpec
from tai42_kit.db import (
    MigrationDiscoveryError,
    MigrationEntry,
    component_binding_declared,
    component_migrator_settings,
)
from tai42_kit.plugins import PLUGIN_SPEC_FILENAME, PluginSpecLoadError, parse_plugin_spec

if TYPE_CHECKING:
    from tai42_contract.manifest import Manifest
    from tai42_kit.clients.settings import PostgresConnectionSettings

    from tai42_skeleton.marketplace.installed_packages import InstalledPackages

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
    """The skeleton chain as a runner entry against the skeleton component's bound migrator identity."""
    return MigrationEntry(
        component=SKELETON_COMPONENT,
        migrations_dir=skeleton_migrations_dir(),
        settings=component_migrator_settings(SKELETON_COMPONENT),
    )


def _normalize_dist(name: str) -> str:
    """PEP 503 normalized distribution name.

    For matching across the hyphen / underscore / dot variants pip treats as one project.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class SkippedChain:
    """A declared chain whose ``migrations_component`` override binding is unset: the component is off.

    The override names an off-unless-declared component, so an unset binding is a valid,
    off state rather than a failure: the chain is not applied anywhere (never under the
    default-database fallback) and every door succeeds while naming it. A DISTINCT surfaced
    outcome, never the silent ``None`` no-migrations one. ``component`` is the override
    identity whose ``TAI_DB_BINDING_*`` turns the component on — carried so the visible
    line names the chain that did not run.
    """

    component: str


@dataclass(frozen=True)
class UnlocatablePackages:
    """An installed distribution whose packages no installed metadata records, so its chain cannot be enumerated.

    An editable install served by an import hook
    (:func:`~tai42_skeleton.marketplace.installed_packages.hook_served_editable`) lists its
    packages in no installed file, so whether it packages a ``tai-plugin.yml`` cannot be read
    without importing it. It is named rather than dropped; once a manifest loads a module of
    it, that module's package is found where the import system serves it and its chain is
    discovered (and gated at boot) like any other.
    """

    distribution: str


@dataclass(frozen=True)
class MigrationChainDiscovery:
    """The outcome of resolving migration chains: the entries to run, the skips, and the unlocatable installs.

    ``entries`` are the runnable chains; ``skipped`` are the declared chains whose
    override binding is unset (surfaced, never silently dropped), so a caller can
    both apply what runs and name what did not; ``unlocatable`` are the installed
    distributions whose packages could not be enumerated, named the same way.
    """

    entries: list[MigrationEntry]
    skipped: list[SkippedChain]
    unlocatable: list[UnlocatablePackages] = field(default_factory=list)


def chain_skip_message(skip: SkippedChain) -> str:
    """The visible line naming an off component's chain: WHICH chain did not run and WHY.

    So an off component is never silent and its store never migrates into the default
    database by fallback.
    """
    return (
        f"the {skip.component!r} component is off — its migrations-component database binding "
        "(TAI_DB_BINDING_*) is not declared, so its migration chain is not applied; declare the "
        "binding to turn the component on and migrate its store to that database."
    )


def unlocatable_message(item: UnlocatablePackages) -> str:
    """The visible line naming an installed distribution whose packages cannot be enumerated, and what covers it."""
    return (
        f"distribution {item.distribution!r} is an editable install served by an import hook; its packages "
        "are recorded in no installed metadata, so a migration chain it declares is discovered only through "
        "a manifest that loads a module of it (run 'tai db migrate' with that manifest, or install it with a "
        "path-based editable mode)."
    )


def _log_chain_skip(skip: SkippedChain) -> None:
    """Log a skipped override chain as a warning, matching the CLI skip line text."""
    logger.warning("db migrate: %s", chain_skip_message(skip))


def chain_component(spec: PluginSpec) -> str | None:
    """The component identity a plugin's chain is recorded under, or ``None`` when it declares no chain.

    The declared ``migrations_component`` override, else the distribution name. ``migrations``
    requires a ``package`` (a cross-field contract rule — a descriptor-only plugin owns no
    packaged SQL), so a chain without one is a loud discovery failure.
    """
    if spec.migrations is None:
        return None
    if spec.package is None:
        raise MigrationDiscoveryError("migrations require a package")
    return spec.migrations_component or spec.package


SettingsSelector = Callable[[str], "PostgresConnectionSettings"]

# A parsed plugin spec and the directory its packaged ``tai-plugin.yml`` is installed in —
# the import-package root its package-relative ``migrations`` path resolves against.
LocatedSpec = tuple[PluginSpec, Traversable]


def plugin_chain(
    spec: PluginSpec,
    *,
    settings_for: SettingsSelector,
    package_root: Traversable,
) -> MigrationEntry | SkippedChain | None:
    """Resolve a plugin's declared chain to one of THREE distinct outcomes.

    An entry, a :class:`SkippedChain` (an override whose component binding is unset — surfaced, never a
    silent ``None``), or ``None`` when the plugin declares no chain.
    ``migrations_component`` (when set) names the database component the chain
    migrates instead of the distribution; it flows into BOTH the entry's ``component``
    history identity AND its ``settings`` connection — one without the other migrates
    the right identity into the wrong database. An override resolves ONLY while its
    binding is EXPLICITLY declared; unset, the chain is skipped rather than run under
    the default-database fallback. With no override the component is the distribution
    name.

    ``settings_for`` selects the connection identity for the component: the migrator
    identity for ``tai db migrate``, the runtime store identity for the boot gate. The
    component and directory are the same either way.

    ``package_root`` is the plugin's import-package root, the directory holding its packaged
    spec; the package-relative ``migrations`` path resolves against it. ``tai db migrate``,
    the installer and the boot gate all pass it, so none imports the plugin and all resolve
    the same folder.
    """
    # Resolved BEFORE the package/directory read so a skipped override chain never needs
    # the feature package's SQL resolved.
    component = chain_component(spec)
    # ``chain_component`` names a component only when ``migrations`` and ``package`` are set.
    if component is None or spec.migrations is None or spec.package is None:
        return None
    if spec.migrations_component is not None and component_binding_declared(component) is None:
        return SkippedChain(component=component)
    migrations_dir = package_root.joinpath(*spec.migrations.split("/"))
    return MigrationEntry(
        component=component,
        migrations_dir=migrations_dir,
        settings=settings_for(component),
    )


def _manifest_loaded_modules(manifest: Manifest) -> list[str]:
    """Every module the component importer loads at boot, in the same set it imports.

    Mirrors the imports :class:`~tai42_skeleton.app.lifecycle.component_import.ComponentImportMixin`
    runs in ``_initialize_components``: the additive roles (lifecycle, webhook-verifier,
    channel, the effective router set, middleware), the four scalar slots
    (backend/sandbox/storage/monitoring), the extension modules, the tool modules, and
    each agent's module. ``connectors`` (pure data, no import path), ``mcp`` servers, and
    ``api_tools`` are not module imports and are not included. So the distributions resolved
    from it are exactly the ones the booted deployment imports, and the ones whose chains the
    boot-time schema gate checks.
    """
    from tai42_skeleton.app.route_defaults import effective_router_modules

    scalar_slots = (
        manifest.backend_module,
        manifest.sandbox_module,
        manifest.storage_module,
        manifest.monitoring_module,
    )
    return [
        *(manifest.lifecycle_modules or []),
        *(manifest.webhook_verifier_modules or []),
        *(manifest.channel_modules or []),
        *effective_router_modules(manifest),
        *(manifest.middlewares_modules or []),
        *(module for module in scalar_slots if module),
        *(manifest.extensions_modules or []),
        *(cfg.module for cfg in manifest.tools),
        *(cfg.module for cfg in manifest.agents),
    ]


def _distributions_for_top_level(top_level: str, installed: InstalledPackages) -> list[str]:
    """The distribution names a top-level import package maps to, de-duplicated.

    From ``installed``, a :func:`~tai42_skeleton.marketplace.compat.distribution_map` snapshot —
    a metadata read that NEVER imports the package, taken once per pass because each read
    scans every installed distribution. A namespace package shared by several distributions
    lists each; an editable or repeated install can list the same distribution twice, so the
    result is de-duplicated on the normalized name while keeping first-seen order.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for dist_name in installed.distributions_for(top_level):
        normalized = _normalize_dist(dist_name)
        if normalized not in seen:
            seen.add(normalized)
            ordered.append(dist_name)
    return ordered


def _located_package_root(top_level: str) -> Traversable | None:
    """The top-level package's directory as its loader serves it, when a ``tai-plugin.yml`` is there; else ``None``.

    ``importlib.util.find_spec`` on a top-level name runs the import finders only — it never
    executes the package. The package's files are then read through its loader's resource
    reader (``get_resource_reader``, the protocol :mod:`importlib.resources` reads a package
    through), so a package served from a source tree, a namespace package and a package a
    finder serves from a virtual tree (meson-python's editable loader) are read alike.
    ``None`` when nothing serves the name, it is a single-file module, its loader serves no
    resources, or no spec is packaged there.
    """
    module_spec = importlib.util.find_spec(top_level)
    if module_spec is None or module_spec.loader is None or module_spec.submodule_search_locations is None:
        return None
    get_reader = getattr(module_spec.loader, "get_resource_reader", None)
    reader = get_reader(module_spec.name) if get_reader is not None else None
    if not isinstance(reader, TraversableResources):
        return None
    root = reader.files()
    return root if root.joinpath(PLUGIN_SPEC_FILENAME).is_file() else None


def _packaged_spec_root(
    dist: importlib.metadata.Distribution, top_level: str, recorded: Sequence[importlib.metadata.PackagePath]
) -> Traversable | None:
    """The directory holding the ``<top_level>/tai-plugin.yml`` ``dist`` packages, or ``None``; never imported.

    ``recorded`` is ``dist``'s
    :func:`~tai42_skeleton.marketplace.installed_packages.recorded_files`. A regular install
    records its package files, so the spec is located off that record; a distribution
    recording files under ``top_level`` but no spec there (a dependency, or one portion of a
    shared namespace package) yields ``None``. An editable install records none of its
    package files, so its spec is located where the import system serves the package
    (:func:`_located_package_root`).
    """
    package_files = [file for file in recorded if file.parts[:1] == (top_level,)]
    if not package_files:
        return _located_package_root(top_level)
    for file in package_files:
        if file.name == PLUGIN_SPEC_FILENAME and file.parts[:-1] == (top_level,):
            return Path(str(dist.locate_file(file))).parent
    return None


def _parse_packaged_spec(package_root: Traversable, dist_name: str) -> PluginSpec:
    """The ``PluginSpec`` packaged in ``package_root`` by distribution ``dist_name``.

    A spec that cannot be read or is malformed is a loud
    :class:`~tai42_kit.db.MigrationDiscoveryError` naming the distribution and the file.
    """
    spec_file = package_root.joinpath(PLUGIN_SPEC_FILENAME)
    try:
        return parse_plugin_spec(spec_file.read_bytes(), source=str(spec_file))
    except (OSError, PluginSpecLoadError, ValidationError) as exc:
        raise MigrationDiscoveryError(
            f"invalid plugin spec packaged in distribution {dist_name!r} at {spec_file}: {exc}"
        ) from exc


def _top_level_spec(top_level: str, installed: InstalledPackages) -> LocatedSpec | None:
    """The spec the distribution of top-level import package ``top_level`` ships, and its package root, import-free.

    The top-level import name is mapped to its distribution(s) through ``installed`` (a
    :func:`~tai42_skeleton.marketplace.compat.distribution_map` snapshot, never an import); the
    first mapped distribution
    that packages ``<top_level>/tai-plugin.yml`` (:func:`_packaged_spec_root`) has that file
    parsed, and the directory holding it is the root its ``migrations`` path resolves
    against — the same resolution :func:`_installed_plugin_specs` gives ``tai db migrate``.
    ``None`` when the top-level maps to no distribution, or no mapped distribution ships the
    spec (an operator-authored module). A spec that EXISTS but is malformed, or whose file
    cannot be read, is a loud :class:`~tai42_kit.db.MigrationDiscoveryError` naming the
    distribution.
    """
    from tai42_skeleton.marketplace.installed_packages import recorded_files

    for dist_name in _distributions_for_top_level(top_level, installed):
        dist = importlib.metadata.distribution(dist_name)
        package_root = _packaged_spec_root(dist, top_level, recorded_files(dist))
        if package_root is not None:
            return _parse_packaged_spec(package_root, dist_name), package_root
    return None


def manifest_plugin_specs(manifest: Manifest, installed: InstalledPackages) -> dict[str, LocatedSpec]:
    """The located spec of every plugin distribution ``manifest`` loads, keyed by normalized distribution name.

    Each module the component importer loads (:func:`_manifest_loaded_modules`) is mapped
    import-free to the spec its distribution packages and the directory holding it
    (:func:`_top_level_spec`); the spec is resolved once per top-level import package, so
    several modules of one plugin collapse onto its one distribution. A module that ships no
    spec (an operator-authored module) contributes nothing. A top-level package whose
    distribution the contract-compat check judges incompatible is not read: the component
    importer refuses that plugin with its compat reason before it loads, and a plugin built
    against another contract may package a spec this one cannot parse. ``installed`` is the
    :func:`~tai42_skeleton.marketplace.compat.distribution_map` snapshot the caller took for
    this pass.
    """
    from tai42_skeleton.marketplace.compat import module_compat

    top_levels = dict.fromkeys(module.partition(".")[0] for module in _manifest_loaded_modules(manifest))
    specs: dict[str, LocatedSpec] = {}
    for top_level in top_levels:
        if module_compat(top_level, installed).status == "incompatible":
            logger.info(
                "schema gate: %r belongs to a contract-incompatible plugin; its spec is not read — the plugin is "
                "refused when it loads",
                top_level,
            )
            continue
        located = _top_level_spec(top_level, installed)
        if located is None or located[0].package is None:
            continue
        specs.setdefault(_normalize_dist(located[0].package), located)
    return specs


def _installed_search_path() -> list[str]:
    """Where installed distributions are looked up: ``sys.path``, then the plugins prefix's site directories.

    The prefix goes last, where :func:`~tai42_skeleton.marketplace.prefix.activate_prefix`
    puts it, so for a distribution present in both the copy the deployment imports is the one
    discovered. The prefix's site directories are added here because a CLI process never
    activates the prefix; a site directory that does not exist yet holds nothing.
    """
    from tai42_skeleton.marketplace.prefix import configured_prefix, prefix_site_dirs

    path = list(sys.path)
    prefix = configured_prefix()
    if prefix is not None:
        path.extend(site for site in prefix_site_dirs(prefix) if site not in path and Path(site).is_dir())
    return path


def _distribution_spec(dist: importlib.metadata.Distribution) -> LocatedSpec | UnlocatablePackages | None:
    """The spec installed distribution ``dist`` packages and the directory holding it, read import-free.

    Each top-level package ``dist`` provides
    (:func:`~tai42_skeleton.marketplace.installed_packages.distribution_top_levels`, editable
    source roots included) is checked for a packaged ``<top_level>/tai-plugin.yml``
    (:func:`_packaged_spec_root`). ``None`` when it packages none (a dependency). An editable
    install served by an import hook records no package to check, so it is an
    :class:`UnlocatablePackages`. Several packaged specs, or a malformed one, is a loud
    :class:`~tai42_kit.db.MigrationDiscoveryError` naming the distribution.
    """
    from tai42_skeleton.marketplace.installed_packages import (
        distribution_top_levels,
        hook_served_editable,
        recorded_files,
    )

    recorded = recorded_files(dist)
    if hook_served_editable(dist, recorded):
        return UnlocatablePackages(distribution=dist.name)
    roots = list(
        {
            str(root): root
            for top_level in distribution_top_levels(dist, recorded)
            if (root := _packaged_spec_root(dist, top_level, recorded)) is not None
        }.values()
    )
    if not roots:
        return None
    if len(roots) > 1:
        raise MigrationDiscoveryError(
            f"distribution {dist.name!r} packages several {PLUGIN_SPEC_FILENAME} files: "
            f"{sorted(str(root.joinpath(PLUGIN_SPEC_FILENAME)) for root in roots)}"
        )
    return _parse_packaged_spec(roots[0], dist.name), roots[0]


def _installed_plugin_specs() -> tuple[list[LocatedSpec], list[UnlocatablePackages]]:
    """The spec and import-package root of every installed distribution that packages a ``tai-plugin.yml``.

    Every distribution on :func:`_installed_search_path` is read from its metadata, never
    imported (:func:`_distribution_spec`); one that packages ``<top_level>/tai-plugin.yml``
    contributes its parsed spec and the directory holding it, against which its
    package-relative ``migrations`` path resolves. A distribution installed twice counts
    once, as the first copy on the search path. Returned with every distribution whose
    packages could not be enumerated.
    """
    seen: set[str] = set()
    found: list[LocatedSpec] = []
    unlocatable: list[UnlocatablePackages] = []
    for dist in importlib.metadata.distributions(path=_installed_search_path()):
        normalized = _normalize_dist(dist.name)
        if normalized in seen:
            continue
        seen.add(normalized)
        located = _distribution_spec(dist)
        if isinstance(located, UnlocatablePackages):
            unlocatable.append(located)
        elif located is not None:
            found.append(located)
    return found, unlocatable


def _persisted_manifest() -> Manifest | None:
    """The persisted manifest, read as boot reads it, or ``None`` when no manifest file exists.

    ``tai db migrate``/``status``/``doctor`` are manifest-optional: with no manifest file,
    ``read_manifest`` raises :class:`FileNotFoundError` and there is simply no manifest-loaded
    plugin — the only case read as absent. A manifest that exists but is unparseable YAML or a
    document :class:`~tai42_skeleton.manifest.Manifest` rejects is a loud
    :class:`~tai42_kit.db.MigrationDiscoveryError` naming the manifest, so the CLI maps it to a
    clean failure; any other fault propagates unwrapped.
    """
    import yaml

    from tai42_skeleton.config import ConfigManagerFactory
    from tai42_skeleton.config.file_manager import FileConfigManager
    from tai42_skeleton.manifest import Manifest

    config_manager = ConfigManagerFactory.create()
    manifest_path = config_manager.manifest_path if isinstance(config_manager, FileConfigManager) else None
    target = f"the configured manifest at {manifest_path}" if manifest_path else "the configured manifest"
    try:
        document = config_manager.read_manifest()
    except FileNotFoundError:
        return None
    except yaml.YAMLError as exc:
        raise MigrationDiscoveryError(f"{target} could not be parsed as YAML: {exc}") from exc
    try:
        return Manifest.model_validate(document)
    except ValidationError as exc:
        raise MigrationDiscoveryError(f"{target} is not a valid manifest: {exc}") from exc


def _discovered_plugin_specs() -> tuple[list[LocatedSpec], list[UnlocatablePackages]]:
    """The installed plugins' specs plus the specs of the plugins the persisted manifest loads.

    The installed scan (:func:`_installed_plugin_specs`) comes first; each plugin the manifest
    route (:func:`manifest_plugin_specs`, the function the boot-time schema gate checks through)
    finds is added unless the scan already holds a spec declaring that distribution, and a
    distribution it finds is no longer unlocatable. So every chain the gate checks for this
    manifest is one migrate applies.
    """
    from tai42_skeleton.marketplace.compat import distribution_map

    specs, unlocatable = _installed_plugin_specs()
    manifest = _persisted_manifest()
    if manifest is None:
        return specs, unlocatable
    declared = {_normalize_dist(spec.package) for spec, _ in specs if spec.package is not None}
    from_manifest = {
        dist_key: located
        for dist_key, located in manifest_plugin_specs(manifest, distribution_map()).items()
        if dist_key not in declared
    }
    found = declared | from_manifest.keys()
    return (
        [*specs, *from_manifest.values()],
        [item for item in unlocatable if _normalize_dist(item.distribution) not in found],
    )


def _resolve_chain_discovery(specs: Iterable[LocatedSpec]) -> MigrationChainDiscovery:
    """Map each spec's :func:`plugin_chain` outcome (migrator identity) to a discovery result.

    A :class:`SkippedChain` (DISTINCT from the ``None`` no-migrations outcome, so a caller reports
    WHICH declared chain did not run rather than silently dropping it) is logged and collected in
    ``skipped``; ``None`` is dropped; each :class:`MigrationEntry` is collected in ``entries``.
    """
    entries: list[MigrationEntry] = []
    skipped: list[SkippedChain] = []
    for spec, package_root in specs:
        outcome = plugin_chain(spec, settings_for=component_migrator_settings, package_root=package_root)
        if isinstance(outcome, SkippedChain):
            _log_chain_skip(outcome)
            skipped.append(outcome)
            continue
        if outcome is not None:
            entries.append(outcome)
    return MigrationChainDiscovery(entries=entries, skipped=skipped)


def discover_plugin_chains() -> MigrationChainDiscovery:
    """Discovery result for every installed plugin that declares a chain, one entry per distribution.

    Every distribution installed in this environment or in the plugins prefix that packages a
    ``tai-plugin.yml`` declaring ``migrations``, whether or not a manifest loads it — the
    schema follows what is installed, so a plugin's tables exist before a manifest change
    loads it — plus every plugin the persisted manifest loads (:func:`_discovered_plugin_specs`).
    Empty when the skeleton database is not configured — with no database there is nowhere to
    migrate. A declared chain whose override binding is unset is carried in ``skipped``, and an
    installed distribution whose packages cannot be enumerated in ``unlocatable``; neither is
    silently dropped. Each plugin chain runs under its own component's bound migrator identity.
    """
    from tai42_kit.db import component_store_configured

    if not component_store_configured(SKELETON_COMPONENT):
        return MigrationChainDiscovery(entries=[], skipped=[])
    specs, unlocatable = _discovered_plugin_specs()
    resolved = _resolve_chain_discovery(specs)
    return MigrationChainDiscovery(entries=resolved.entries, skipped=resolved.skipped, unlocatable=unlocatable)


def installed_plugin_entry(distribution: str) -> MigrationEntry | None:
    """The runner entry for installed distribution ``distribution``'s chain, resolved import-free.

    The marketplace installer's resolution, after ``pip`` lands a plugin: the distribution is
    found on :func:`_installed_search_path` and its packaged spec located and parsed
    (:func:`_distribution_spec`), exactly as ``tai db migrate`` finds it, so the folder the
    installer applies is the folder migrate applies and boot checks. ``None`` when the
    packaged spec declares no chain, or declares an override whose binding is unset (a skip
    logged as a visible warning). A distribution that is not installed, packages no spec, or
    is an editable install whose packages no metadata records, is a loud
    :class:`~tai42_kit.db.MigrationDiscoveryError` naming why.
    """
    dist = next(iter(importlib.metadata.distributions(name=distribution, path=_installed_search_path())), None)
    if dist is None:
        raise MigrationDiscoveryError(
            f"distribution {distribution!r} is not installed; its migrations cannot be located"
        )
    located = _distribution_spec(dist)
    if isinstance(located, UnlocatablePackages):
        raise MigrationDiscoveryError(unlocatable_message(located))
    if located is None:
        raise MigrationDiscoveryError(
            f"distribution {distribution!r} packages no {PLUGIN_SPEC_FILENAME}; its migrations cannot be located"
        )
    entries = _resolve_chain_discovery([located]).entries
    return entries[0] if entries else None


def installed_plugin_entries() -> list[MigrationEntry]:
    """Runner entries for every installed plugin that declares a chain, dropping the skips."""
    return discover_plugin_chains().entries


def discover_all_migration_chains() -> MigrationChainDiscovery:
    """Discovery result for every chain this process is responsible for, each under its bound identity.

    The two skeleton-owned chains (the skeleton baseline and the ``states`` record store) are always
    present in ``entries`` — neither declares a ``migrations_component`` override, so neither skips —
    followed by every plugin chain :func:`discover_plugin_chains` finds; ``skipped`` carries every
    declared plugin chain whose override binding is unset and ``unlocatable`` every installed
    distribution whose packages cannot be enumerated. The skeleton chains are resolved BEFORE plugin discovery so an
    unresolvable skeleton migrator identity (an unconfigured or half-set-admin database) raises
    eagerly, before any installed distribution is read.
    """
    from tai42_skeleton.states.db import states_entry

    skeleton_chains = [skeleton_entry(), states_entry()]
    plugin = discover_plugin_chains()
    return MigrationChainDiscovery(
        entries=[*skeleton_chains, *plugin.entries],
        skipped=plugin.skipped,
        unlocatable=plugin.unlocatable,
    )


def all_migration_entries() -> list[MigrationEntry]:
    """Every chain this process is responsible for, each under its component's bound identity.

    The two skeleton-owned chains (the skeleton baseline and the ``states`` record store) plus every
    installed plugin chain.
    """
    return discover_all_migration_chains().entries
