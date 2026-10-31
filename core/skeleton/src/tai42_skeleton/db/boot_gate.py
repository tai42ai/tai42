"""Boot-time schema gate — refuse to build an epoch on an out-of-date database.

Every epoch build (process boot and every reload) asserts, before any manifest module
imports, that every migration chain the deployment depends on is fully applied: the
skeleton chain, the ``states`` chain, and the chain each plugin the deployment loads declares.
A pending migration or a checksum mismatch is one loud refusal naming every stale chain and
``tai db migrate``, never a half-migrated serve. Each chain is read on its component's OWN
runtime store connection (the runner grants ``SELECT`` on ``tai_schema_history`` to
``PUBLIC``), so a lesser-privileged runtime role can verify chain state.

The refusal itself is the shared kit primitive :func:`~tai42_kit.db.assert_chain_applied`;
this module chooses the chains and supplies the ``tai db migrate`` remediation wording. A
plugin chain resolves through the same :func:`~tai42_skeleton.db.discovery.plugin_chain`
``tai db migrate`` uses, against the directory the plugin's packaged spec is installed in,
so the gate checks exactly what migrate applies and never imports the plugin.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tai42_kit.db import (
    MigrationEntry,
    SchemaOutOfDateError,
    assert_chain_applied,
    component_store_configured,
    component_store_settings,
)

from tai42_skeleton.db.discovery import (
    SKELETON_COMPONENT,
    SkippedChain,
    chain_component,
    chain_skip_message,
    manifest_plugin_specs,
    plugin_chain,
    skeleton_migrations_dir,
)

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

    from tai42_contract.manifest import Manifest

    from tai42_skeleton.marketplace.installed_packages import InstalledPackages

logger = logging.getLogger(__name__)

# The user-facing fix for a pending/diverged chain. Kit's shared gate primitive appends it
# verbatim; the wording is stable and part of the contract.
_REMEDIATION = "Run 'tai db migrate' to apply the pending migrations, then restart."

# Re-exported so importers of the gate keep a single ``SchemaOutOfDateError`` symbol
# regardless of which module they reach for; the class itself lives in kit.
__all__ = ["SchemaOutOfDateError", "assert_chain_applied", "assert_deployment_schemas_applied"]


def _log_unconfigured(component: str) -> None:
    logger.info("schema gate: the %r database is not configured — skipping its migration check", component)


def _platform_entries() -> list[MigrationEntry]:
    """The skeleton and ``states`` chains whose component database is configured.

    A deployment with no database configured for a component owns none of its tables, so
    its chain is not checked.
    """
    from tai42_skeleton.states.db import STATES_COMPONENT, states_migrations_dir

    chains: tuple[tuple[str, Traversable], ...] = (
        (SKELETON_COMPONENT, skeleton_migrations_dir()),
        (STATES_COMPONENT, states_migrations_dir()),
    )
    entries: list[MigrationEntry] = []
    for component, migrations_dir in chains:
        if not component_store_configured(component):
            _log_unconfigured(component)
            continue
        entries.append(
            MigrationEntry(
                component=component,
                migrations_dir=migrations_dir,
                settings=component_store_settings(component),
            )
        )
    return entries


def _loaded_plugin_entries(manifest: Manifest, installed: InstalledPackages) -> list[MigrationEntry]:
    """The chain of every plugin a deployment of ``manifest`` loads, on each component's runtime store.

    A plugin declaring no chain contributes nothing. A chain under the distribution's own
    identity is checked when that component's database is configured. A
    ``migrations_component`` override whose binding is undeclared means the feature is off:
    the skip is logged with the line ``tai db migrate`` reports, and nothing is checked. A
    declared override is checked on its bound database. A malformed packaged spec raises
    :class:`~tai42_kit.db.MigrationDiscoveryError` naming the distribution.
    """
    entries: list[MigrationEntry] = []
    for spec, package_root in manifest_plugin_specs(manifest, installed).values():
        component = chain_component(spec)
        if component is None:
            continue
        if spec.migrations_component is None and not component_store_configured(component):
            _log_unconfigured(component)
            continue
        outcome = plugin_chain(spec, settings_for=component_store_settings, package_root=package_root)
        if isinstance(outcome, SkippedChain):
            logger.warning("schema gate: %s", chain_skip_message(outcome))
            continue
        if outcome is not None:
            entries.append(outcome)
    return entries


async def assert_deployment_schemas_applied(manifest: Manifest, installed: InstalledPackages) -> None:
    """Epoch-build gate: assert every chain the deployment of ``manifest`` depends on is applied.

    The platform chains (skeleton, ``states``) and the chain of every plugin a deployment of
    ``manifest`` loads, checked together so one :class:`SchemaOutOfDateError` names every stale chain.
    ``installed`` is the :func:`~tai42_skeleton.marketplace.compat.distribution_map` snapshot
    the build takes after the plugins prefix is on ``sys.path``. Run in ``BootMixin.start()``
    before any store read and before any manifest module imports, so a pending chain refuses
    with the ``tai db migrate`` fix and a plugin never runs against tables its chain has not
    created.
    """
    entries = [*_platform_entries(), *_loaded_plugin_entries(manifest, installed)]
    await assert_chain_applied(entries, remediation=_REMEDIATION)
