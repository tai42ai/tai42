"""The preflight every door that imports a manifest's modules runs first.

Two doors import manifest-named modules into a process: the epoch build
(``BootMixin.start``, every boot and every reload) and ``tai setup --recover``. Both run
:func:`prepare_manifest_imports` before the first import, so both see the plugins installed
in the plugin layer, both refuse a loaded plugin whose required platform feature is off,
both refuse an out-of-date database with the ``tai db migrate`` fix instead of failing deeper
on a missing table, and the first call in a process refuses plugin environments that do not
match the install store with the ``tai plugins rebuild`` fix. Later calls (reloads, epoch
rebuilds) do not verify the environments against the store again; every call refuses a plugin
layer whose current build record is missing or does not read, in the verification's words.
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
    """Make ``manifest``'s plugins importable, gate them, return the package→dist snapshot.

    In order:

    1. Make the plugins directory's current environments visible
       (:func:`~tai42_skeleton.marketplace.environments.activate.activate_plugins_dir`): the
       plugin layer's current build goes at the END of ``sys.path`` and the console scripts every
       server package declares are exposed to the stdio launch; a no-op past the declaration when
       no plugins directory is configured.
    2. Take one :func:`~tai42_skeleton.marketplace.compat.distribution_map` snapshot. It reads the
       layer's current build record for the plugins that build left out, so a current build
       without its record, or with one that does not read, refuses here — before both gates —
       with the verification's own finding
       (:func:`~tai42_skeleton.marketplace.environments.current_record.current_record`) as
       :class:`~tai42_skeleton.marketplace.compat.CorePluginBootError`.
    3. The requirement gate: a required platform feature that is off or unknown raises
       :class:`~tai42_skeleton.marketplace.compat.CorePluginBootError`
       (:func:`~tai42_skeleton.app.lifecycle.plugin_requirements.refuse_unmet_plugin_requirements`).
    4. The schema gate (:func:`~tai42_skeleton.db.boot_gate.assert_deployment_schemas_applied`)
       raises :class:`~tai42_kit.db.SchemaOutOfDateError` on a pending or diverged chain — the
       skeleton's, the ``states`` chain, or one a loaded plugin declares.
    5. On the first call in the process,
       :func:`~tai42_skeleton.marketplace.environments.boot_verification.verify_environments_at_boot`
       verifies the plugin environments against the install store and raises
       :class:`~tai42_skeleton.marketplace.compat.CorePluginBootError` on a mismatch; it runs after
       the schema gate so a pending skeleton chain refuses before the install store is read, and
       later calls do not verify.

    Both gates read the packaged spec of every plugin the deployment loads — the manifest's
    module imports, its ``studio_plugins`` packages and the selected config provider —
    import-free (:func:`~tai42_skeleton.db.discovery.manifest_plugin_specs`). The returned
    snapshot serves every compat verdict of the imports that follow. The marketplace imports are
    function-local to keep the app import chain free of the marketplace package.
    """
    from tai42_skeleton.marketplace.compat import CorePluginBootError, distribution_map
    from tai42_skeleton.marketplace.environments.activate import activate_plugins_dir
    from tai42_skeleton.marketplace.environments.boot_verification import verify_environments_at_boot
    from tai42_skeleton.marketplace.errors import CurrentRecordError

    activate_plugins_dir()
    try:
        installed = distribution_map()
    except CurrentRecordError as exc:
        raise CorePluginBootError(exc.finding.boot_text()) from exc
    refuse_unmet_plugin_requirements(
        spec for spec, _package_root in manifest_plugin_specs(manifest, installed).values()
    )
    await assert_deployment_schemas_applied(manifest, installed)
    await verify_environments_at_boot()
    return installed
