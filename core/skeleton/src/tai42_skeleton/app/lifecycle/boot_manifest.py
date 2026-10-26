"""Read the boot/live manifest and bridge the stored env into the process at boot."""

import logging
import os

from tai42_kit.utils.data.env_markers import scan_env_marker_refs

from tai42_skeleton.app.bus import WorkerKind
from tai42_skeleton.app.lifecycle.state import LifecycleState
from tai42_skeleton.manifest import Manifest

logger = logging.getLogger(__name__)


class BootManifestMixin(LifecycleState):
    """Boot/live manifest reads and the stored-env bridge into the process."""

    async def _read_boot_manifest(self, *, kind: WorkerKind) -> Manifest:
        """The one cold-boot manifest read every serving/backend entrypoint crosses.

        Bridges the persisted env store into ``os.environ`` and builds this process's one worker
        bus (worker kind ``kind``) under it BEFORE the manifest is resolved, so a manifest
        ``!ENV ${VAR}`` whose var lives only in the store resolves to its real value at boot
        rather than the silent ``"N/A"`` sentinel; reads + validates the manifest under the
        bridged env; then names any marker that STILL dangles. The ASGI serve worker, the
        stdio server, and the backend worker all boot through here, so the bridge holds on
        every serving door with no per-door copy — the same store the in-process reload path
        reads.
        """
        await self._apply_stored_env_at_boot(kind=kind)
        manifest = Manifest.model_validate(self._config_manager.read_manifest())
        self._warn_dangling_boot_env_markers()
        return manifest

    async def _apply_stored_env_at_boot(self, *, kind: WorkerKind) -> None:
        """Apply the persisted env store into ``os.environ`` at boot and build the worker bus under it.

        Captures the deployment env first: nothing has applied a stored key to ``os.environ``
        yet, so the capture is the env the process was started with. Then applies the store
        through the SAME apply-then-reset primitive an in-process reload uses, judged by this
        process's own deployment env: a key it sets keeps its value (the stored copy is ignored
        and named in a WARNING), every other stored key is applied, and any setting cached
        before this call is re-resolved under the bridged env. Builds this process's one bus
        under the applied env, so a bus configured only in the store is the bus it joins, and
        reads the fleet's census on it once: the stored copies of keys another worker's
        deployment sets are withdrawn (named in a WARNING) before the manifest is read and
        before anything is bound or built on them. A withdraw that moves a value that locates
        the bus (its Redis URL or namespace) off the bus this process is a member of refuses the
        boot (:func:`~tai42_skeleton.app.boot_rules.check_bus_stays_deployment_given`); one that
        moves only how the process takes part (the heartbeat, a timeout) builds the bus again
        under the withdrawn env. When the bus cannot answer the census read, the bridge judges
        by this process's own deployment env and logs a WARNING naming the operation; the first
        join reads the census again and withdraws what it then learns the fleet owns. A busless
        process's census is its own row. An absent store (no ``.env`` / no Secret — the
        managers' shared ``FileNotFoundError``) is an empty store, not a fault: zero keys
        applied. Only a genuinely unreadable store raises. The boot env handlers then re-apply
        every process-wide effect set up from settings read before the bridge (the root logger
        level, fastmcp's settings and logger; the connector-secret log rule follows each
        settings reset instead), so each follows the applied env; a raising handler propagates.
        Every registered respawn-class setting is then bound to the resulting effective env.
        """
        from tai42_kit.settings import bind_respawn_class_env

        from tai42_skeleton.app.applied_env import (
            applied_env_keys,
            apply_env_and_reset_settings,
            withdraw_fleet_owned_env,
        )
        from tai42_skeleton.app.boot_rules import check_bus_stays_deployment_given
        from tai42_skeleton.app.bus_settings import bus_settings
        from tai42_skeleton.config.deployment_env import (
            DeploymentOwnership,
            capture_deployment_env,
            resolve_deployment_ownership,
        )

        try:
            stored = self._config_manager.read_env()
        except FileNotFoundError:
            stored = {}
        capture_deployment_env()
        apply_env_and_reset_settings(stored, DeploymentOwnership())
        self._bus = self._build_bus(kind)
        ownership = await resolve_deployment_ownership(self._bus, "boot env bridge")
        before = bus_settings()
        withdrawn = withdraw_fleet_owned_env(ownership)
        if withdrawn:
            after = bus_settings()
            check_bus_stays_deployment_given(member=self._bus.settings, before=before, after=after, withdrawn=withdrawn)
            if after != self._bus.settings:
                # The withdraw moved how this process takes part in the same fleet: the bus it
                # joins is built under the withdrawn env.
                self._bus = self._build_bus(kind)
        # Every process-wide effect set up from settings read before this bridge follows the
        # env it applied.
        self._fire_boot_env_applied()
        # The env just applied is the env every registered setting's resource is built under; a
        # group that registers later binds its own values at its registration. A later reload
        # names the recycle-class keys whose value has moved off the bound one.
        bind_respawn_class_env()
        logger.info(
            "boot env bridge: applied %d stored-env key(s) into the process environment", len(applied_env_keys())
        )

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
