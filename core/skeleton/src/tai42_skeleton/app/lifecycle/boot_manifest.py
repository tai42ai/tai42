"""Read the boot/live manifest and bridge the stored env into the process at boot."""

import logging
import os

from tai42_kit.utils.data.env_markers import scan_env_marker_refs

from tai42_skeleton.app.lifecycle.state import LifecycleState
from tai42_skeleton.manifest import Manifest

logger = logging.getLogger(__name__)


class BootManifestMixin(LifecycleState):
    """Boot/live manifest reads and the stored-env bridge into the process."""

    def _read_boot_manifest(self) -> Manifest:
        """The one cold-boot manifest read every serving/backend entrypoint crosses.

        Bridges the persisted env store into ``os.environ`` BEFORE the manifest is
        resolved, so a manifest ``!ENV ${VAR}`` whose var lives only in the store
        resolves to its real value at boot rather than the silent ``"N/A"`` sentinel;
        reads + validates the manifest under the bridged env; then names any marker
        that STILL dangles. The ASGI serve worker, the stdio server, and the backend
        worker all boot through here, so the bridge holds on every serving door with
        no per-door copy — the same store the in-process reload path reads.
        """
        self._apply_stored_env_at_boot()
        manifest = Manifest.model_validate(self._config_manager.read_manifest())
        self._warn_dangling_boot_env_markers()
        return manifest

    def _apply_stored_env_at_boot(self) -> None:
        """Apply the persisted env store into ``os.environ`` at boot.

        Goes through the SAME apply-then-reset primitive an in-process reload uses,
        so boot and reload resolve settings under the store identically: stored env
        OVERRIDES container env, and any setting cached before this call is
        re-resolved under the bridged env. An absent store (no ``.env`` / no Secret —
        the managers' shared ``FileNotFoundError``) is an empty store, not a fault:
        zero keys applied. Only a genuinely unreadable store raises.
        """
        from tai42_skeleton.app.epoch import apply_env_and_reset_settings

        try:
            stored = self._config_manager.read_env()
        except FileNotFoundError:
            stored = {}
        apply_env_and_reset_settings(stored)
        logger.info("boot env bridge: applied %d stored-env key(s) into the process environment", len(stored))

    def _warn_dangling_boot_env_markers(self) -> None:
        """After the boot env bridge, name any manifest ``!ENV ${VAR}`` marker still dangling.

        A dangling marker's var is in NEITHER the container env nor the store — it
        silently resolved to the ``"N/A"`` sentinel. One loud WARNING listing the
        dangling var NAMES (never the marker values); boot proceeds, since the
        env-write boundary is the hard gate against INTRODUCING a dangling marker.
        Reads the PRESERVED manifest so the marker expressions survive unresolved and
        their var names can be named.
        """
        try:
            preserved = self._config_manager.read_manifest_preserved()
        except FileNotFoundError:
            return
        dangling = sorted(
            {ref.var for ref in scan_env_marker_refs(preserved) if ref.required and ref.var not in os.environ}
        )
        if dangling:
            logger.warning(
                'boot env bridge: %d manifest !ENV marker(s) resolve to the "N/A" sentinel — their env '
                "var is set in neither the container env nor the stored env: %s",
                len(dangling),
                ", ".join(dangling),
            )

    def _require_live_manifest(self) -> Manifest:
        """The live in-process manifest, or a loud error if the app is not started.

        The typed accessor behind ``app.admin.live_manifest_typed``.
        """
        manifest = self._manifest
        if manifest is None:
            raise RuntimeError("TaiMCP is not started — call start()/app_context first.")
        return manifest

    def _refresh_manifest_mcp(self) -> None:
        """Graft the manifest's current MCP rows into the boot-time snapshot.

        A row written after boot becomes reloadable / a removed one deregisterable.
        When no external manifest is readable (embedded/test runtimes) the in-memory
        copy stays authoritative.
        """
        try:
            fresh = Manifest.model_validate(self._config_manager.read_manifest())
        except FileNotFoundError:
            logger.warning(
                "no external manifest to re-read; using the in-memory MCP rows",
                exc_info=True,
            )
            return
        if self._manifest is None:
            raise RuntimeError("TaiMCP is not started — call start()/app_context first.")
        self._manifest.replace_mcp(fresh.mcp)
