"""``tai plugins rebuild`` — rebuild the plugin environments from the install store.

It runs in the deployment block (:func:`~tai42_skeleton.cli.native.host_boot.deployment`): the
boot read a server runs, so a setting kept only in the env store resolves as it does for a server,
and the process app bound as a server binds it. Under the fleet-wide
marketplace lock, it verifies every environment under ``TAI_PLUGINS_DIR`` against the install
store and this image, rebuilds each one that fails
(:func:`~tai42_skeleton.marketplace.environments.rebuild.rebuild_environments`), deletes
the unused builds and the server environments no plugin is recorded in, and prints one line
per environment. It needs the deployment's env and manifest, its database settings,
``TAI_PLUGINS_DIR``, pip and the package index. A refusal is rendered by
:func:`~tai42_skeleton.cli.native.host_boot.run_host_command`: its text and each note added to
it, and a pip failure's report with it, then a non-zero exit.
"""

from __future__ import annotations

import typer
from tai42_cli.render import render_table

from tai42_skeleton.cli.native.host_boot import deployment, run_host_command
from tai42_skeleton.marketplace.environments.rebuild import Rebuild, rebuild_environments
from tai42_skeleton.marketplace.environments.settings import configured_plugins_dir, require_plugins_dir
from tai42_skeleton.marketplace.locks import fleet_lock
from tai42_skeleton.marketplace.pip import run_pip
from tai42_skeleton.marketplace.store import MarketplaceInstallStore

NEXT_START = "running servers load the rebuilt plugin layer on their next start"


async def _rebuild() -> Rebuild:
    async with deployment():
        plugins_dir = require_plugins_dir(configured_plugins_dir())
        async with fleet_lock():
            rows = await MarketplaceInstallStore().list_installed()
            return await rebuild_environments(plugins_dir, rows, pip_runner=run_pip)


def rebuild() -> None:
    """Rebuild every plugin environment that does not match the install store or this image.

    Prints each environment as "up to date", "rebuilt" or "removed". Exits non-zero, with
    the reason, when the plugins directory is unset or unwritable, another marketplace
    operation holds the lock, the database is unreachable, the config provider is absent,
    the manifest does not exist, or a build fails.

    Example: ``tai plugins rebuild``
    """
    result = run_host_command(_rebuild())
    records = [{"environment": o.label, "result": o.action, "detail": o.detail} for o in result.outcomes]
    typer.echo(render_table(records, ["environment", "result", "detail"]))
    if result.layer_rebuilt_with_plugins:
        typer.echo(NEXT_START)
