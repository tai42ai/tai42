"""Discovery of the migration chains a TAI process owns.

The skeleton's own chain lives at a fixed packaged path and is registered
directly. A table-owning plugin declares its chain OPT-IN via the contract's
``migrations`` field (a package-relative directory); its component identity is its
pip distribution name. Installed plugins are discovered from THREE sources: the
marketplace install-attribution store; the plugins prefix scanned for distributions
shipping a packaged ``tai-plugin.yml`` (a plugin pre-installed into the prefix by pip
has no store row but its declared chain must still run); and the effective manifest's
loaded modules (a plugin baked into the environment and named by the manifest has
neither a store row nor a prefix, yet the deployment imports it at boot and gates its
schema — so the runner must see it too, or the app boot-gates a chain the migrator
never ran). This module turns those declarations into :class:`~tai42_kit.db.MigrationEntry` values
the runner consumes — resolving a plugin's packaged directory through
``importlib.resources`` (or directly against its prefix install) and failing loudly
when a declared directory is absent from the installed package.

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
class SkippedChain:
    """A declared chain whose ``migrations_component`` override binding is unset.

    A DISTINCT surfaced skip, never the silent ``None`` no-migrations outcome.
    ``component`` is the override identity whose ``TAI_DB_BINDING_*`` the operator
    must declare before its store migrates anywhere — carried so the visible skip
    line names the chain that did not run.
    """

    component: str


@dataclass(frozen=True)
class MigrationChainDiscovery:
    """The outcome of resolving migration chains: the entries to run and the skips.

    ``entries`` are the runnable chains; ``skipped`` are the declared chains whose
    override binding is unset (surfaced, never silently dropped), so a caller can
    both apply what runs and name what did not.
    """

    entries: list[MigrationEntry]
    skipped: list[SkippedChain]


def chain_skip_message(skip: SkippedChain) -> str:
    """The visible line naming a skipped override chain: WHICH chain did not run and WHY.

    So an optional feature's store never silently migrates into the default database by fallback.
    """
    return (
        f"skipping the {skip.component!r} migration chain — its migrations-component database "
        "binding (TAI_DB_BINDING_*) is not declared; set it so this component's store migrates "
        "to the intended database."
    )


def _log_chain_skip(skip: SkippedChain) -> None:
    """Log a skipped override chain as a warning, matching the CLI skip line text."""
    logger.warning("db migrate: %s", chain_skip_message(skip))


def _plugin_chain(spec: PluginSpec, *, package_root: Traversable | None = None) -> MigrationEntry | SkippedChain | None:
    """Resolve a plugin's declared chain to one of THREE distinct outcomes.

    An entry to run, a :class:`SkippedChain` (an override whose component binding is unset — surfaced, never a
    silent ``None``), or ``None`` when the plugin declares no chain.
    ``migrations_component`` (when set) names the database component the chain
    migrates instead of the distribution; it flows into BOTH the entry's ``component``
    history identity AND its ``settings`` connection — one without the other migrates
    the right identity into the wrong database. An override runs ONLY while its
    binding is EXPLICITLY declared; unset, the chain is skipped rather than run under
    the default-database fallback. With no override the component is the distribution
    name.

    ``package_root`` (when given) is the plugin's import-package root; the
    package-relative ``migrations`` path resolves against it directly. The
    prefix-scan source passes it because a prefix-installed distribution need not be
    importable from this process's ``sys.path``. ``None`` resolves the root from
    installed-package metadata, as a store-attributed environment install is.
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
        return SkippedChain(component=component)
    if package_root is None:
        package = _import_package_for_distribution(spec.package)
        package_root = importlib.resources.files(package)
    migrations_dir = package_root.joinpath(*spec.migrations.split("/"))
    return MigrationEntry(
        component=component,
        migrations_dir=migrations_dir,
        settings=component_migrator_settings(component),
    )


def plugin_migration_entry(spec: PluginSpec) -> MigrationEntry | None:
    """A plugin's chain as a runner entry, or ``None`` when it declares none or its override binding is unset.

    An unset ``migrations_component`` override is a skip surfaced by a visible line — an undeclared binding
    never migrates into the default database.
    The component is the declared ``migrations_component`` when set, else the plugin's
    distribution name (``spec.package``); its connection is that component's bound
    migrator identity. The ``migrations`` path is resolved against the plugin's import
    root; the runner then enforces that the directory actually exists and holds a
    well-formed chain.
    """
    outcome = _plugin_chain(spec)
    if isinstance(outcome, SkippedChain):
        _log_chain_skip(outcome)
        return None
    return outcome


def _prefix_plugin_spec_paths() -> dict[str, Path]:
    """The packaged ``tai-plugin.yml`` path of every distribution installed in the plugins prefix.

    Keyed by normalized distribution name.
    Scans the prefix's OWN site directories (never the running environment), so a
    plugin pre-installed into the prefix by pip is discovered without a marketplace
    install record and without the prefix being activated on ``sys.path`` — a CLI
    process never activates it. Empty when no prefix is configured, the prefix is
    empty, or no installed distribution ships a spec; a dependency distribution
    (no packaged spec) is skipped. A distribution shipping several spec files is
    malformed — a loud discovery failure, never a guess between them.
    """
    from tai42_skeleton.marketplace.prefix import configured_prefix, prefix_site_dirs

    prefix = configured_prefix()
    if prefix is None:
        return {}
    site_dirs = [site for site in prefix_site_dirs(prefix) if Path(site).is_dir()]
    if not site_dirs:
        return {}
    found: dict[str, Path] = {}
    for dist in importlib.metadata.distributions(path=site_dirs):
        specs = [file for file in dist.files or [] if file.name == PLUGIN_SPEC_FILENAME]
        if not specs:
            continue
        if len(specs) > 1:
            raise MigrationDiscoveryError(
                f"distribution {dist.name!r} in the plugins prefix ships several {PLUGIN_SPEC_FILENAME} files: "
                f"{sorted(str(file) for file in specs)}"
            )
        found[_normalize_dist(dist.name)] = Path(str(dist.locate_file(specs[0])))
    return found


async def _collect_store_sources() -> dict[str, tuple[PluginSpec, Path | None]]:
    """The marketplace install-attribution store's rows as source slots, keyed ``store:{index}``.

    Each row's stored ``PluginSpec`` is validated; an invalid stored spec is a
    loud :class:`MigrationDiscoveryError`. A missing ``marketplace_installs``
    table (a database not yet migrated to the skeleton baseline that creates it)
    yields no rows, never a discovery crash. Store rows carry no package root —
    their chains resolve against the running environment's installed metadata.
    """
    from psycopg.errors import UndefinedTable

    from tai42_skeleton.marketplace.store import MarketplaceInstallStore

    try:
        records = await MarketplaceInstallStore().list_installed()
    except UndefinedTable:
        # The skeleton baseline creates the table; before it, there are no
        # marketplace-attributed installs to read.
        records = []
    sources: dict[str, tuple[PluginSpec, Path | None]] = {}
    for index, record in enumerate(records):
        try:
            spec = PluginSpec.model_validate(record.spec)
        except ValidationError as exc:
            package = record.spec.get("package") if isinstance(record.spec, dict) else None
            raise MigrationDiscoveryError(
                f"invalid plugin spec in the marketplace install store for distribution {package!r}: {exc}"
            ) from exc
        sources[f"store:{index}"] = (spec, None)
    return sources


def _merge_prefix_sources(
    sources: dict[str, tuple[PluginSpec, Path | None]],
) -> dict[str, tuple[PluginSpec, Path | None]]:
    """Fold every prefix-scanned ``tai-plugin.yml`` into ``sources`` as a ``prefix:{dist}`` slot.

    Resolves its chain from the prefix filesystem.
    A prefix hit for a distribution a ``store:`` slot also names is the SAME
    installed artifact: the store slot is dropped so the distribution yields one
    entry, resolved from the prefix. An invalid prefix spec is a loud
    :class:`MigrationDiscoveryError`. Returns the combined map.
    """
    for dist_name, spec_path in _prefix_plugin_spec_paths().items():
        for key, (stored, _) in list(sources.items()):
            if stored.package is not None and _normalize_dist(stored.package) == dist_name:
                del sources[key]
        try:
            spec = parse_plugin_spec(spec_path.read_bytes(), source=str(spec_path))
        except (OSError, PluginSpecLoadError, ValidationError) as exc:
            raise MigrationDiscoveryError(
                f"invalid plugin spec at {spec_path} (distribution {dist_name!r} in the plugins prefix): {exc}"
            ) from exc
        sources[f"prefix:{dist_name}"] = (spec, spec_path.parent)
    return sources


def _manifest_loaded_modules(manifest: Manifest) -> list[str]:
    """Every module the component importer loads at boot, in the same set it imports.

    Mirrors the imports :class:`~tai42_skeleton.app.lifecycle.component_import.ComponentImportMixin`
    runs in ``_initialize_components``: the additive roles (lifecycle, webhook-verifier,
    channel, the effective router set, middleware), the four scalar slots
    (backend/sandbox/storage/monitoring), the extension modules, the tool modules, and
    each agent's module. ``connectors`` (pure data, no import path), ``mcp`` servers, and
    ``api_tools`` are not module imports and are not included. So the distributions this
    source resolves are exactly the ones the booted deployment imports and boot-gates.
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


def _distributions_for_top_level(top_level: str) -> list[str]:
    """The distribution names a top-level import package maps to, de-duplicated.

    From ``importlib.metadata.packages_distributions()`` — a metadata read that NEVER
    imports the package. A namespace package shared by several distributions lists each;
    an editable or repeated install can list the same distribution twice, so the result
    is de-duplicated on the normalized name while keeping first-seen order.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for dist_name in importlib.metadata.packages_distributions().get(top_level, []):
        normalized = _normalize_dist(dist_name)
        if normalized not in seen:
            seen.add(normalized)
            ordered.append(dist_name)
    return ordered


def _distribution_spec_file(dist_name: str, top_level: str) -> importlib.metadata.PackagePath | None:
    """The ``<top_level>/tai-plugin.yml`` file a distribution packages, or ``None``.

    Located among the distribution's recorded files (``Distribution.files``) — never by
    importing it. Returns the ``PackagePath``, which the caller reads off the recorded
    install location; a distribution that ships no such file (a dependency, or a
    distribution whose namespace package lives elsewhere) yields ``None``.
    """
    for file in importlib.metadata.distribution(dist_name).files or []:
        if file.name == PLUGIN_SPEC_FILENAME and file.parts[:-1] == (top_level,):
            return file
    return None


def _manifest_module_spec(module: str) -> PluginSpec | None:
    """The ``PluginSpec`` a manifest-loaded module's distribution ships, resolved import-free.

    Resolved from installed metadata WITHOUT importing the module or its package.
    The module's top-level import name is mapped to its distribution(s) via
    ``importlib.metadata.packages_distributions()`` (a metadata read, never an import);
    the first mapped distribution that packages ``<top_level>/tai-plugin.yml`` has that
    file read straight off its recorded files. ``None`` when the top-level maps to no
    distribution, or no mapped distribution ships the spec (an operator-authored
    module) — the same quiet no-spec outcome the manifest source relies on. A spec that
    EXISTS but is malformed, or whose recorded file cannot be read, is a loud
    :class:`~tai42_kit.db.MigrationDiscoveryError` naming the distribution.
    """
    top_level = module.partition(".")[0]
    for dist_name in _distributions_for_top_level(top_level):
        spec_file = _distribution_spec_file(dist_name, top_level)
        if spec_file is None:
            continue
        try:
            return parse_plugin_spec(spec_file.read_binary(), source=f"{top_level}/{PLUGIN_SPEC_FILENAME}")
        except (OSError, PluginSpecLoadError, ValidationError) as exc:
            raise MigrationDiscoveryError(
                f"invalid plugin spec packaged in distribution {dist_name!r} at "
                f"{top_level}/{PLUGIN_SPEC_FILENAME}: {exc}"
            ) from exc
    return None


def _collect_manifest_sources() -> dict[str, tuple[PluginSpec, Path | None]]:
    """The chains the effective manifest LOADS, as source slots keyed ``manifest:{dist}``.

    The effective manifest is resolved exactly as boot resolves it —
    ``Manifest.model_validate(config_manager.read_manifest())`` — so the runner enumerates
    the same modules the booted deployment imports and gates on. Each loaded module is
    mapped to the ``tai-plugin.yml`` packaged beside its top-level import package WITHOUT
    importing the module: its top-level import name is resolved to a distribution through
    installed metadata and the packaged spec is read off that distribution's recorded files
    (:func:`_manifest_module_spec`). Importing a consumer's package runs its module-level
    code (which may touch the unbound app), so the CLI must never import it to discover its
    chain. A module that ships no spec (an operator-authored deployment module) yields
    nothing. The chain resolves from installed metadata, so the slot carries NO package root
    (the manifest module is installed in this process, unlike a prefix-only install). One
    slot per distribution: several modules of one plugin collapse onto its distribution key.

    ``tai db migrate``/``status``/``doctor`` are manifest-OPTIONAL: when no manifest file
    exists, ``read_manifest`` raises :class:`FileNotFoundError` and this source contributes
    NOTHING -- a plugin can be "loaded via the manifest" only when a manifest exists, so with
    no manifest there is simply no manifest-loaded chain to gate on, and discovery falls
    through to the store and prefix sources. This is the absence of a source, not a degrade:
    the ONLY swallowed case is the file being absent. A manifest that EXISTS but cannot be
    read as a valid manifest -- unparseable YAML (``read_manifest`` raises
    :class:`yaml.YAMLError`) or a document ``Manifest.model_validate`` rejects
    (:class:`pydantic.ValidationError`) -- is a real error, wrapped as a loud
    :class:`~tai42_kit.db.MigrationDiscoveryError` naming the manifest so the CLI seam maps it
    to a clean credential-free failure rather than a raw traceback (a ``YAMLError`` is not a
    ``ValueError``, so unwrapped it would escape that seam). Any other failure propagates
    unwrapped so a genuinely unexpected fault is never masked.
    """
    import yaml

    from tai42_skeleton.config import ConfigManagerFactory
    from tai42_skeleton.manifest import Manifest

    config_manager = ConfigManagerFactory.create()
    manifest_path = getattr(config_manager, "_manifest_path", None)
    target = f"the configured manifest at {manifest_path}" if manifest_path else "the configured manifest"
    try:
        document = config_manager.read_manifest()
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as exc:
        raise MigrationDiscoveryError(f"{target} could not be parsed as YAML: {exc}") from exc
    try:
        manifest = Manifest.model_validate(document)
    except ValidationError as exc:
        raise MigrationDiscoveryError(f"{target} is not a valid manifest: {exc}") from exc
    sources: dict[str, tuple[PluginSpec, Path | None]] = {}
    for module in _manifest_loaded_modules(manifest):
        spec = _manifest_module_spec(module)
        if spec is None or spec.package is None:
            continue
        sources[f"manifest:{_normalize_dist(spec.package)}"] = (spec, None)
    return sources


def _merge_manifest_sources(
    sources: dict[str, tuple[PluginSpec, Path | None]],
) -> dict[str, tuple[PluginSpec, Path | None]]:
    """Fold every chain the effective manifest LOADS into ``sources`` as a ``manifest:{dist}`` slot.

    A distribution a root-carrying source (the prefix scan) already covers keeps that
    source — it resolves the chain from the prefix filesystem, which a CLI process cannot
    reach through ``sys.path``. A distribution covered only by a rootless ``store:`` slot is
    the SAME installed artifact: the store slot is dropped so the distribution yields one
    entry, resolved from the manifest-loaded package's installed metadata. Returns the
    combined map.
    """
    rooted_dists = {
        _normalize_dist(spec.package)
        for spec, root in sources.values()
        if root is not None and spec.package is not None
    }
    for key, slot in _collect_manifest_sources().items():
        dist_name = key.split(":", 1)[1]
        if dist_name in rooted_dists:
            continue
        for existing_key, (stored, _) in list(sources.items()):
            if stored.package is not None and _normalize_dist(stored.package) == dist_name:
                del sources[existing_key]
        sources[key] = slot
    return sources


def _resolve_chain_discovery(sources: dict[str, tuple[PluginSpec, Path | None]]) -> MigrationChainDiscovery:
    """Map each source's ``_plugin_chain`` outcome to a discovery result.

    A :class:`SkippedChain` (DISTINCT from the ``None`` no-migrations outcome, so a caller reports
    WHICH declared chain did not run rather than silently dropping it) is logged and collected in
    ``skipped``; ``None`` is dropped; each :class:`MigrationEntry` is collected in ``entries``.
    """
    entries: list[MigrationEntry] = []
    skipped: list[SkippedChain] = []
    for spec, package_root in sources.values():
        outcome = _plugin_chain(spec, package_root=package_root)
        if isinstance(outcome, SkippedChain):
            _log_chain_skip(outcome)
            skipped.append(outcome)
            continue
        if outcome is not None:
            entries.append(outcome)
    return MigrationChainDiscovery(entries=entries, skipped=skipped)


def _resolve_chain_entries(sources: dict[str, tuple[PluginSpec, Path | None]]) -> list[MigrationEntry]:
    """The runnable entries of :func:`_resolve_chain_discovery`, dropping the skips."""
    return _resolve_chain_discovery(sources).entries


async def discover_plugin_chains() -> MigrationChainDiscovery:
    """Discovery result for every installed plugin that declares a chain.

    From THREE sources, one entry per distribution:

    - the marketplace install-attribution store (the local record of every
      marketplace-installed plugin and the exact ``PluginSpec`` it shipped);
    - the plugins prefix, scanned for installed distributions shipping a packaged
      ``tai-plugin.yml`` — a plugin pre-installed into the prefix by pip has no
      store row, but its declared chain must still run;
    - the effective manifest's loaded modules — a plugin baked into the environment
      and named by the manifest has neither a store row nor a prefix, yet the
      deployment imports it at boot and gates its schema, so the runner must see it
      too (resolved exactly as boot resolves the manifest).

    A plugin present in several sources (a marketplace install into a configured
    prefix, a manifest module that is also installed) is the same installed artifact
    and yields ONE entry; a source carrying a package root (the prefix copy, which
    needs no ``sys.path`` activation in a CLI process) is preferred. Empty when the
    skeleton database is not configured — with no database there is nowhere to
    migrate. A declared chain whose override binding is unset is carried in
    ``skipped``, never silently dropped. Each plugin chain runs under its own
    component's bound migrator identity.
    """
    from tai42_kit.db import component_store_configured

    if not component_store_configured(SKELETON_COMPONENT):
        return MigrationChainDiscovery(entries=[], skipped=[])
    sources = await _collect_store_sources()
    sources = _merge_prefix_sources(sources)
    sources = _merge_manifest_sources(sources)
    return _resolve_chain_discovery(sources)


async def installed_plugin_entries() -> list[MigrationEntry]:
    """Runner entries for every installed plugin that declares a chain, dropping the skips."""
    return (await discover_plugin_chains()).entries


async def discover_all_migration_chains() -> MigrationChainDiscovery:
    """Discovery result for every chain this process is responsible for, each under its bound identity.

    The two skeleton-owned chains (the skeleton baseline and the ``states`` record store) are always
    present in ``entries`` — neither declares a ``migrations_component`` override, so neither skips —
    followed by every installed plugin chain; ``skipped`` carries every declared plugin chain whose
    override binding is unset. The skeleton chains are resolved BEFORE plugin discovery so an
    unresolvable skeleton migrator identity (an unconfigured or half-set-admin database) raises
    eagerly, before plugin discovery opens a connection to enumerate the store.
    """
    from tai42_skeleton.states.db import states_entry

    skeleton_chains = [skeleton_entry(), states_entry()]
    plugin = await discover_plugin_chains()
    return MigrationChainDiscovery(
        entries=[*skeleton_chains, *plugin.entries],
        skipped=plugin.skipped,
    )


async def all_migration_entries() -> list[MigrationEntry]:
    """Every chain this process is responsible for, each under its component's bound identity.

    The two skeleton-owned chains (the skeleton baseline and the ``states`` record store) plus every
    installed plugin chain.
    """
    return (await discover_all_migration_chains()).entries
