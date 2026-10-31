"""The preflight every door that imports a manifest's modules runs first.

Two doors import manifest-named modules into a process: the epoch build
(``BootMixin.start``, every boot and every reload) and ``tai setup --recover``. Both run
:func:`prepare_manifest_imports` before the first import, so both see plugins installed in
the plugins prefix, both refuse a loaded plugin whose required platform feature is off, and
both refuse an out-of-date database with the ``tai db migrate`` fix instead of failing deeper
on a missing table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_skeleton.app.lifecycle.plugin_requirements import refuse_unmet_plugin_requirements
from tai42_skeleton.db.boot_gate import assert_deployment_schemas_applied
from tai42_skeleton.db.discovery import manifest_plugin_specs

if TYPE_CHECKING:
    from tai42_contract.manifest import Manifest

    from tai42_skeleton.marketplace.installed_packages import InstalledPackages


async def prepare_manifest_imports(manifest: Manifest) -> InstalledPackages:
    """Make ``manifest``'s plugins importable, gate their requirements and schema, return the package→dist snapshot.

    Puts the configured plugin prefix on ``sys.path`` (appended at the END, so the
    environment shadows the prefix for anything present in both; a no-op when no prefix is
    configured) and declares the stdio launch (the plugin script locations and what a script
    launched from the prefix imports, :func:`~tai42_skeleton.marketplace.prefix.activate_prefix`),
    takes one :func:`~tai42_skeleton.marketplace.compat.distribution_map` snapshot after that,
    and runs two gates on it. Both read the packaged spec of every plugin the deployment
    loads — the manifest's module imports, its ``studio_plugins`` packages and the selected
    config provider — import-free (:func:`~tai42_skeleton.db.discovery.manifest_plugin_specs`).
    In the requirement gate a required platform feature that is off or unknown raises
    :class:`~tai42_skeleton.marketplace.compat.CorePluginBootError`
    (:func:`~tai42_skeleton.app.lifecycle.plugin_requirements.refuse_unmet_plugin_requirements`).
    The schema gate (:func:`~tai42_skeleton.db.boot_gate.assert_deployment_schemas_applied`)
    raises :class:`~tai42_kit.db.SchemaOutOfDateError` on a pending or diverged chain — the
    skeleton's, the ``states`` chain, or one a loaded plugin declares. The returned
    snapshot serves every compat verdict of the imports that follow. The marketplace imports
    are function-local to keep the app import chain free of the marketplace package.
    """
    from tai42_skeleton.marketplace.compat import distribution_map
    from tai42_skeleton.marketplace.prefix import activate_prefix

    activate_prefix()
    installed = distribution_map()
    refuse_unmet_plugin_requirements(
        spec for spec, _package_root in manifest_plugin_specs(manifest, installed).values()
    )
    await assert_deployment_schemas_applied(manifest, installed)
    return installed
