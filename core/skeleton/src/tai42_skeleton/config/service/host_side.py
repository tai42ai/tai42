"""The pipeline collaborators of a host-side process: a process with no serving surface.

A command that runs on the deployment host with no server (``tai plugins uninstall --offline``)
writes the manifest through the same :class:`~tai42_skeleton.config.service.ConfigService` write
stage a server runs, wired by :meth:`~tai42_skeleton.config.service.ConfigService.host_side`. It has
nothing to reload and no extension registered, so its admin and its combo check say so.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tai42_contract.manifest import ExtensionElement, TaiMCPConfig


class HostSideAdmin:
    """The reload seam of a host-side process.

    It serves nothing, so neither method is reached by a manifest write that only removes
    entries; a call is a wiring error and raises.
    """

    def reload_config(self) -> dict[str, Any]:
        """Refuse: a host-side process has nothing to reload."""
        raise RuntimeError(
            "a host-side process serves nothing to reload; the deployment loads the written manifest at its next start"
        )

    async def served_mcp_names(self, config: TaiMCPConfig) -> list[str]:
        """Refuse: a host-side process serves no MCP server to list."""
        raise RuntimeError(
            "a host-side process cannot list the MCP servers a running server serves; "
            "run this change against the running server"
        )


def host_side_combo_check(tool: str, combo: Sequence[ExtensionElement]) -> None:
    """Leave every combo to the bind at the deployment's next start.

    A host-side process registers no extension, so every combo is the case the live check
    leaves to the bind after the reload: a combo naming an unregistered extension.
    """
