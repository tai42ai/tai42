"""The host-side half of ``tai plugins uninstall --offline`` — uninstall one plugin with no server.

It runs where the deployment runs, with the deployment's env, so it holds the same trust level as
reading the boot log. In the deployment block
(:func:`~tai42_skeleton.cli.native.host_boot.deployment`: the one boot read, which bridges the
stored env and builds the worker bus, and the process app bound as a server binds it) it runs the one uninstall flow
(:meth:`~tai42_skeleton.marketplace.installer.Installer.uninstall`) under the fleet lock with the
host-side config pipeline (:meth:`~tai42_skeleton.config.service.ConfigService.host_side`): the
install-store row, the plugin environments and the manifest change exactly as a running server's
uninstall changes them. Nothing reloads here: a server loads the uninstall on its next start, and a
server that is running keeps what it loaded until then. A refusal is rendered by
:func:`~tai42_skeleton.cli.native.host_boot.run_host_command`: its text and each note added to
it, such as the unwind's note that the previous plugin environment was restored.
"""

from __future__ import annotations

from typing import Any

from tai42_skeleton.cli.native.host_boot import deployment, run_host_command
from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.marketplace import manifest_apply
from tai42_skeleton.marketplace.installer import Installer

NEXT_START = "a server loads the uninstall on its next start"


def uninstall(ref: str) -> dict[str, Any]:
    """Uninstall ``ref`` on this deployment host with no server and return the receipt payload."""
    return run_host_command(_uninstall(ref))


async def _uninstall(ref: str) -> dict[str, Any]:
    async with deployment():
        installer = Installer(
            config_service=ConfigService.host_side(),
            converge=manifest_apply.converge_at_next_start,
        )
        receipt = await installer.uninstall(ref)
    return {
        "ref": receipt["ref"],
        "uninstalled": True,
        "manifest_written": receipt["manifest_written"],
        "notes": [*receipt["notes"], NEXT_START],
    }
