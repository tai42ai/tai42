"""The preflight every door that imports a manifest's modules runs first.

Two doors import manifest-named modules into a process: the epoch build
(``BootMixin.start``, every boot and every reload) and ``tai setup --recover``. Both run
:func:`prepare_manifest_imports` before the first import, so both see plugins installed in
the plugins prefix and both refuse an out-of-date database with the ``tai db migrate`` fix
instead of failing deeper on a missing table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_skeleton.db.boot_gate import assert_deployment_schemas_applied

if TYPE_CHECKING:
    from tai42_contract.manifest import Manifest

    from tai42_skeleton.marketplace.installed_packages import InstalledPackages


async def prepare_manifest_imports(manifest: Manifest) -> InstalledPackages:
    """Make ``manifest``'s plugins importable, gate the schema, and return the package→dist snapshot.

    Puts the configured plugin prefix on ``sys.path`` (appended at the END, so the
    environment shadows the prefix for anything present in both; a no-op when no prefix is
    configured), takes one :func:`~tai42_skeleton.marketplace.compat.distribution_map`
    snapshot after that, and runs the schema gate
    (:func:`~tai42_skeleton.db.boot_gate.assert_deployment_schemas_applied`) on it: a
    pending or diverged chain — the skeleton's, the ``states`` chain, or one a loaded plugin
    declares — raises :class:`~tai42_kit.db.SchemaOutOfDateError`. The returned snapshot
    serves every compat verdict of the imports that follow. The marketplace imports are
    function-local to keep the app import chain free of the marketplace package.
    """
    from tai42_skeleton.marketplace.compat import distribution_map
    from tai42_skeleton.marketplace.prefix import activate_prefix

    activate_prefix()
    installed = distribution_map()
    await assert_deployment_schemas_applied(manifest, installed)
    return installed
